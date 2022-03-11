# -*- coding: utf-8 -*-
# file: boosting_aug_core.py
# time: 2021/12/20
# author: yangheng <yangheng@m.scnu.edu.cn>
# github: https://github.com/yangheng95
# Copyright (C) 2021. All Rights Reserved.
import copy
import itertools
import random
import shutil
import time
import os
import numpy as np
import torch
import tqdm

from findfile import find_cwd_files, find_cwd_dir, find_cwd_dirs, find_dir, find_files, find_dirs, find_file
from pyabsa import APCConfigManager, APCCheckpointManager, TextClassifierCheckpointManager, APCModelList
from pyabsa.core.apc.prediction.sentiment_classifier import SentimentClassifier
from pyabsa.utils.pyabsa_utils import retry

from termcolor import colored

from pyabsa.functional import Trainer
from pyabsa.functional.dataset import DatasetItem
from pyabsa.functional.dataset.dataset_manager import download_datasets_from_github, ABSADatasetList, ClassificationDatasetList
from transformers import BertForMaskedLM, DebertaV2ForMaskedLM, AutoConfig, AutoTokenizer, RobertaForMaskedLM

from pyabsa import ClassificationConfigManager, BERTClassificationModelList, ClassificationDatasetList

from boost_aug import __version__


def rename(src, tgt):
    if os.path.exists(tgt):
        remove(tgt)
    os.rename(src, tgt)


def remove(p):
    if os.path.exists(p):
        os.remove(p)


@retry
def get_mlm_and_tokenizer(sent_classifier, config):
    if isinstance(sent_classifier, SentimentClassifier):
        base_model = sent_classifier.model.bert.base_model
    else:
        base_model = sent_classifier.bert.base_model
    pretrained_config = AutoConfig.from_pretrained(config.pretrained_bert)
    if 'deberta-v3' in config.pretrained_bert:
        MLM = DebertaV2ForMaskedLM(pretrained_config).to(sent_classifier.opt.device)
        MLM.deberta = base_model
    elif 'roberta' in config.pretrained_bert:
        MLM = RobertaForMaskedLM(pretrained_config).to(sent_classifier.opt.device)
        MLM.roberta = base_model
    else:
        MLM = BertForMaskedLM(pretrained_config).to(sent_classifier.opt.device)
        MLM.bert = base_model
    return MLM, AutoTokenizer.from_pretrained(config.pretrained_bert)


class AugmentBackend:
    EDA = 'EDA'
    ContextualWordEmbsAug = 'ContextualWordEmbsAug'
    RandomWordAug = 'RandomWordAug'
    AntonymAug = 'AntonymAug'
    SplitAug = 'SplitAug'
    BackTranslationAug = 'BackTranslationAug'
    SpellingAug = 'SpellingAug'


class BoostingAug:

    def __init__(self,
                 ROOT: str = '',
                 BOOSTING_FOLD=4,
                 CLASSIFIER_TRAINING_NUM=2,
                 CONFIDENCE_THRESHOLD=0.99,
                 AUGMENT_NUM_PER_CASE=10,
                 WINNER_NUM_PER_CASE=10,
                 PERPLEXITY_THRESHOLD=5,
                 AUGMENT_PCT=0.1,
                 AUGMENT_BACKEND=AugmentBackend.EDA,
                 USE_CONFIDENCE=True,
                 USE_PERPLEXITY=True,
                 USE_LABEL=True,
                 device='cuda'
                 ):
        """

        :param ROOT: The path to save intermediate checkpoint
        :param BOOSTING_FOLD: Number of splits in crossing boosting augment
        :param CLASSIFIER_TRAINING_NUM: Number of pre-trained inference model using for confidence calculation
        :param CONFIDENCE_THRESHOLD: Confidence threshold used for augmentations filtering
        :param AUGMENT_NUM_PER_CASE: Number of augmentations per example
        :param WINNER_NUM_PER_CASE: Number of selected augmentations per example
        :param PERPLEXITY_THRESHOLD: Perplexity threshold used for augmentations filtering
        :param AUGMENT_PCT: Word change probability used in backend augment method
        :param AUGMENT_BACKEND: Augmentation backend used for augmentations generation, e.g., EDA, ContextualWordEmbsAug
        """

        assert hasattr(AugmentBackend, AUGMENT_BACKEND)
        if not ROOT or not os.path.exists(ROOT):
            self.ROOT = os.getenv('$HOME') if os.getenv('$HOME') else os.getcwd()
        else:
            self.ROOT = ROOT

        self.BOOSTING_FOLD = BOOSTING_FOLD
        self.CLASSIFIER_TRAINING_NUM = CLASSIFIER_TRAINING_NUM
        self.CONFIDENCE_THRESHOLD = CONFIDENCE_THRESHOLD
        self.AUGMENT_NUM_PER_CASE = AUGMENT_NUM_PER_CASE if AUGMENT_NUM_PER_CASE > 0 else 1
        self.WINNER_NUM_PER_CASE = WINNER_NUM_PER_CASE
        self.PERPLEXITY_THRESHOLD = PERPLEXITY_THRESHOLD
        self.AUGMENT_PCT = AUGMENT_PCT
        self.AUGMENT_BACKEND = AUGMENT_BACKEND
        self.USE_CONFIDENCE = USE_CONFIDENCE
        self.USE_PERPLEXITY = USE_PERPLEXITY
        self.USE_LABEL = USE_LABEL
        self.device = device

        if self.AUGMENT_BACKEND in 'EDA':
            # Here are some augmenters from https://github.com/QData/TextAttack
            from textattack.augmentation import EasyDataAugmenter as Aug
            # Alter default values if desired
            self.augmenter = Aug(pct_words_to_swap=self.AUGMENT_PCT, transformations_per_example=self.AUGMENT_NUM_PER_CASE)
        else:
            # Here are some augmenters from https://github.com/makcedward/nlpaug
            import nlpaug.augmenter.word as naw
            if self.AUGMENT_BACKEND in 'ContextualWordEmbsAug':
                self.augmenter = naw.ContextualWordEmbsAug(
                    model_path='roberta-base', action="substitute", aug_p=self.AUGMENT_PCT, device=self.device)
            elif self.AUGMENT_BACKEND in 'RandomWordAug':
                self.augmenter = naw.RandomWordAug(action="swap")
            elif self.AUGMENT_BACKEND in 'AntonymAug':
                self.augmenter = naw.AntonymAug()
            elif self.AUGMENT_BACKEND in 'SplitAug':
                self.augmenter = naw.SplitAug()
            elif self.AUGMENT_BACKEND in 'BackTranslationAug':
                self.augmenter = naw.BackTranslationAug(from_model_name='facebook/wmt19-en-de',
                                                        to_model_name='facebook/wmt19-de-en',
                                                        device=self.device
                                                        )
            elif self.AUGMENT_BACKEND in 'SpellingAug':
                self.augmenter = naw.SpellingAug()

    def get_apc_config(self, config):

        config.BOOSTING_FOLD = self.BOOSTING_FOLD
        config.CLASSIFIER_TRAINING_NUM = self.CLASSIFIER_TRAINING_NUM
        config.CONFIDENCE_THRESHOLD = self.CONFIDENCE_THRESHOLD
        config.AUGMENT_NUM_PER_CASE = self.AUGMENT_NUM_PER_CASE
        config.WINNER_NUM_PER_CASE = self.WINNER_NUM_PER_CASE
        config.PERPLEXITY_THRESHOLD = self.PERPLEXITY_THRESHOLD
        config.AUGMENT_PCT = self.AUGMENT_PCT
        config.AUGMENT_TOOL = self.AUGMENT_BACKEND
        config.BoostAugVersion = __version__

        apc_config_english = copy.deepcopy(config)
        apc_config_english.cache_dataset = False
        apc_config_english.patience = 10
        apc_config_english.log_step = -1
        apc_config_english.model = APCModelList.FAST_LCF_BERT
        apc_config_english.pretrained_bert = 'microsoft/deberta-v3-base'
        apc_config_english.SRD = 3
        apc_config_english.lcf = 'cdw'
        apc_config_english.use_bert_spc = True
        apc_config_english.learning_rate = 1e-5
        apc_config_english.batch_size = 16
        apc_config_english.num_epoch = 25
        apc_config_english.embed_dim = 768
        apc_config_english.hidden_dim = 768
        apc_config_english.log_step = -1
        apc_config_english.evaluate_begin = 5
        apc_config_english.l2reg = 1e-8
        apc_config_english.cross_validate_fold = -1  # disable cross_validate
        apc_config_english.seed = [random.randint(0, 10000) for _ in range(2)]
        return apc_config_english

    def get_tc_config(self, config):
        config.BOOSTING_FOLD = self.BOOSTING_FOLD
        config.CLASSIFIER_TRAINING_NUM = self.CLASSIFIER_TRAINING_NUM
        config.CONFIDENCE_THRESHOLD = self.CONFIDENCE_THRESHOLD
        config.AUGMENT_NUM_PER_CASE = self.AUGMENT_NUM_PER_CASE
        config.WINNER_NUM_PER_CASE = self.WINNER_NUM_PER_CASE
        config.PERPLEXITY_THRESHOLD = self.PERPLEXITY_THRESHOLD
        config.AUGMENT_PCT = self.AUGMENT_PCT
        config.AUGMENT_TOOL = self.AUGMENT_BACKEND
        config.BoostAugVersion = __version__

        tc_config_english = copy.deepcopy(config)
        tc_config_english.max_seq_len = 80
        tc_config_english.dropout = 0
        tc_config_english.model = BERTClassificationModelList.BERT
        tc_config_english.pretrained_bert = 'microsoft/deberta-v3-base'
        tc_config_english.optimizer = 'adam'
        tc_config_english.cache_dataset = False
        tc_config_english.patience = 10
        tc_config_english.hidden_dim = 768
        tc_config_english.embed_dim = 768
        tc_config_english.log_step = -1
        tc_config_english.learning_rate = 1e-5
        tc_config_english.batch_size = 16
        tc_config_english.num_epoch = 25
        tc_config_english.evaluate_begin = 5
        tc_config_english.l2reg = 1e-8
        tc_config_english.cross_validate_fold = -1  # disable cross_validate
        tc_config_english.seed = [random.randint(0, 10000) for _ in range(2)]
        return tc_config_english

    def tc_boost_free_training(self, config: APCConfigManager,
                               dataset: DatasetItem,
                               task='text_classification',
                               ):
        if not isinstance(dataset, DatasetItem) and os.path.exists(dataset):
            dataset = DatasetItem(dataset)
        prepare_dataset_and_clean_env(dataset.dataset_name, task, rewrite_cache=True)

        return Trainer(config=config,
                       dataset=dataset,  # train set and test set will be automatically detected
                       checkpoint_save_mode=0,  # =None to avoid save model
                       auto_device=self.device  # automatic choose CUDA or CPU
                       )

    def tc_classic_boost_training(self, config: APCConfigManager,
                                  dataset: DatasetItem,
                                  rewrite_cache=True,
                                  task='text_classification',
                                  train_after_aug=False
                                  ):
        if not isinstance(dataset, DatasetItem) and os.path.exists(dataset):
            dataset = DatasetItem(dataset)
        _config = self.get_tc_config(config)
        if 'pretrained_bert' in _config.args:
            tag = '{}_{}_{}'.format(_config.model.__name__.lower(), dataset.dataset_name, os.path.basename(_config.pretrained_bert))
        else:
            tag = '{}_{}_{}'.format(_config.model.__name__.lower(), dataset.dataset_name, 'glove')
        if rewrite_cache:
            prepare_dataset_and_clean_env(dataset.dataset_name, task, rewrite_cache)

        train_data = []
        for dataset_file in detect_dataset(dataset, task)['train']:
            print('processing {}'.format(dataset_file))
            fin = open(dataset_file, encoding='utf8', mode='r')
            lines = fin.readlines()
            fin.close()
            for i in tqdm.tqdm(range(0, len(lines))):
                lines[i] = lines[i].strip()
                train_data.append([lines[i]])
        fs = find_files(self.ROOT, [tag, '.augment.ignore'])
        if self.WINNER_NUM_PER_CASE:

            fout_aug_train = open('{}/classic.train.{}.augment'.format(os.path.dirname(dataset_file), tag), encoding='utf8', mode='w')

            for item in tqdm.tqdm(train_data, postfix='Classic Augmenting...'):

                item[0] = item[0].replace('$LABEL$', 'PLACEHOLDER')
                label = item[0].split('PLACEHOLDER')[1].strip()

                if self.AUGMENT_BACKEND in 'EDA':
                    augs = self.augmenter.augment(item[0])
                else:
                    augs = self.augmenter.augment(item[0], n=self.AUGMENT_NUM_PER_CASE, num_thread=os.cpu_count())

                if isinstance(augs, str):
                    augs = [augs]
                for aug in augs:
                    if aug.endswith('PLACEHOLDER {}'.format(label)):
                        _text = aug.replace('PLACEHOLDER', '$LABEL$')
                        fout_aug_train.write(_text + '\n')

            fout_aug_train.close()

        post_clean(os.path.dirname(dataset_file))

        if train_after_aug:
            print(colored('Start classic augment training...', 'cyan'))
            return Trainer(config=config,
                           dataset=dataset,  # train set and test set will be automatically detected
                           auto_device=self.device  # automatic choose CUDA or CPU
                           ).load_trained_model()

    def tc_cross_boost_training(self, config: ClassificationConfigManager,
                                dataset: DatasetItem,
                                rewrite_cache=True,
                                task='text_classification',
                                train_after_aug=False
                                ):
        if not isinstance(dataset, DatasetItem) and os.path.exists(dataset):
            dataset = DatasetItem(dataset)
        _config = self.get_tc_config(config)
        if 'pretrained_bert' in _config.args:
            tag = '{}_{}_{}'.format(_config.model.__name__.lower(), dataset.dataset_name, os.path.basename(_config.pretrained_bert))
        else:
            tag = '{}_{}_{}'.format(_config.model.__name__.lower(), dataset.dataset_name, 'glove')

        prepare_dataset_and_clean_env(dataset.dataset_name, task, rewrite_cache)

        for valid_file in detect_dataset(dataset, task)['valid']:
            rename(valid_file, valid_file + '.ignore')

        data = []
        dataset_file = ''
        dataset_files = detect_dataset(dataset, task)['train']

        for dataset_file in dataset_files:
            print('processing {}'.format(dataset_file))
            fin = open(dataset_file, encoding='utf8', mode='r')
            lines = fin.readlines()
            fin.close()
            rename(dataset_file, dataset_file + '.ignore')
            for i in tqdm.tqdm(range(0, len(lines))):
                lines[i] = lines[i].strip()

                data.append([lines[i]])

        train_data = data
        len_per_fold = len(train_data) // self.BOOSTING_FOLD + 1
        folds = [train_data[i: i + len_per_fold] for i in range(0, len(train_data), len_per_fold)]

        if not os.path.exists('checkpoints/cross_boost/{}_{}'.format(config.model.__name__.lower(), dataset.dataset_name)):
            os.makedirs('checkpoints/cross_boost/{}_{}'.format(config.model.__name__.lower(), dataset.dataset_name))

        for fold_id, b_idx in enumerate(range(len(folds))):
            print(colored('boosting... No.{} in {} folds'.format(b_idx + 1, self.BOOSTING_FOLD), 'red'))
            f = find_file(self.ROOT, [tag, '{}.'.format(fold_id), '.augment.ignore'])
            if f:
                rename(f, f.replace('.ignore', ''))
                continue
            train_data = list(itertools.chain(*[x for i, x in enumerate(folds) if i != b_idx]))
            valid_data = folds[b_idx]

            fout_train = open('{}/train.dat.tmp'.format(os.path.dirname(dataset_file), fold_id), encoding='utf8', mode='w')
            fout_boost = open('{}/valid.dat.tmp'.format(os.path.dirname(dataset_file), fold_id), encoding='utf8', mode='w')
            for case in train_data:
                for line in case:
                    fout_train.write(line + '\n')

            for case in valid_data:
                for line in case:
                    fout_boost.write(line + '\n')

            fout_train.close()
            fout_boost.close()

            keys = ['checkpoint', 'cross_boost', dataset.dataset_name, 'deberta', 'No.{}'.format(b_idx + 1)]

            if len(find_dirs(self.ROOT, keys)) < self.CLASSIFIER_TRAINING_NUM + 1:
                Trainer(config=_config,
                        dataset=dataset,  # train set and test set will be automatically detected
                        checkpoint_save_mode=1,
                        path_to_save='checkpoints/cross_boost/{}/No.{}'.format(tag, b_idx + 1),
                        auto_device=self.device  # automatic choose CUDA or CPU
                        )

            torch.cuda.empty_cache()
            time.sleep(5)

            checkpoint_path = ''
            max_f1 = ''
            for path in find_dirs(self.ROOT, keys):
                if 'f1' in path and path[path.index('f1'):] > max_f1:
                    max_f1 = max(path[path.index('f1'):], checkpoint_path)
                    checkpoint_path = path

            sent_classifier = TextClassifierCheckpointManager.get_text_classifier(checkpoint_path, auto_device=self.device)
            sent_classifier.opt.eval_batch_size = 128

            MLM, tokenizer = get_mlm_and_tokenizer(sent_classifier, _config)

            dataset_files = detect_dataset(dataset, task)
            boost_sets = dataset_files['valid']
            augmentations = []
            perplexity_list = []
            confidence_list = []

            for boost_set in boost_sets:
                print('Augmenting -> {}'.format(boost_set))
                fin = open(boost_set, encoding='utf8', mode='r')
                lines = fin.readlines()
                fin.close()
                remove(boost_set)
                for i in tqdm.tqdm(range(0, len(lines)), postfix='Augmenting...'):

                    lines[i] = lines[i].strip().replace('$LABEL$', 'PLACEHOLDER')
                    label = lines[i].split('PLACEHOLDER')[1].strip()

                    if self.AUGMENT_BACKEND in 'EDA':
                        raw_augs = self.augmenter.augment(lines[i])
                    else:
                        raw_augs = self.augmenter.augment(lines[i], n=self.AUGMENT_NUM_PER_CASE, num_thread=os.cpu_count())

                    if isinstance(raw_augs, str):
                        raw_augs = [raw_augs]
                    augs = {}
                    for text in raw_augs:
                        if text.endswith('PLACEHOLDER {}'.format(label)):
                            with torch.no_grad():
                                results = sent_classifier.infer(text.replace('PLACEHOLDER', '!ref!'), print_result=False)
                                ids = tokenizer(text.replace('PLACEHOLDER', '{}'.format(label)), return_tensors="pt")
                                ids['labels'] = ids['input_ids'].clone()
                                ids = ids.to(self.device)
                                loss = MLM(**ids)['loss']
                                perplexity = torch.exp(loss / ids['input_ids'].size(1))

                                perplexity_list.append(perplexity.item())
                                confidence_list.append(results[0]['confidence'])
                                if self.USE_LABEL:
                                    if results[0]['ref_check'] != 'Correct':
                                        continue

                                if self.USE_CONFIDENCE:
                                    if results[0]['confidence'] <= self.CONFIDENCE_THRESHOLD:
                                        continue

                                augs[perplexity.item()] = [text.replace('PLACEHOLDER', '$LABEL$')]

                    if self.USE_CONFIDENCE:
                        key_rank = sorted(augs.keys())
                    else:
                        key_rank = list(augs.keys())
                    for key in key_rank[:self.WINNER_NUM_PER_CASE]:
                        if self.USE_PERPLEXITY:
                            if key < self.PERPLEXITY_THRESHOLD:
                                augmentations += augs[key]
                        else:
                            augmentations += augs[key]

            print('Avg Confidence: {} Max Confidence: {} Min Confidence: {}'.format(np.average(confidence_list), max(confidence_list), min(confidence_list)))

            print('Avg Perplexity: {} Max Perplexity: {} Min Perplexity: {}'.format(np.average(perplexity_list), max(perplexity_list), min(perplexity_list)))

            fout = open('{}/{}.cross_boost.{}.train.augment.ignore'.format(os.path.dirname(dataset_file), fold_id, tag), encoding='utf8', mode='w')

            for line in augmentations:
                fout.write(line + '\n')
            fout.close()

            del sent_classifier
            del MLM

            torch.cuda.empty_cache()
            time.sleep(5)

            post_clean(os.path.dirname(dataset_file))

        for f in find_cwd_files('.ignore'):
            rename(f, f.replace('.ignore', ''))

        if train_after_aug:
            print(colored('Start cross boosting augment...', 'green'))
            return Trainer(config=config,
                           dataset=dataset,  # train set and test set will be automatically detected
                           checkpoint_save_mode=0,  # =None to avoid save model
                           auto_device=self.device  # automatic choose CUDA or CPU
                           )

    def tc_mono_boost_training(self, config: ClassificationConfigManager,
                               dataset: DatasetItem,
                               rewrite_cache=True,
                               task='text_classification',
                               train_after_aug=False
                               ):
        if not isinstance(dataset, DatasetItem) and os.path.exists(dataset):
            dataset = DatasetItem(dataset)
        _config = self.get_tc_config(config)
        if 'pretrained_bert' in _config.args:
            tag = '{}_{}_{}'.format(_config.model.__name__.lower(), dataset.dataset_name, os.path.basename(_config.pretrained_bert))
        else:
            tag = '{}_{}_{}'.format(_config.model.__name__.lower(), dataset.dataset_name, 'glove')

        prepare_dataset_and_clean_env(dataset.dataset_name, task, rewrite_cache)

        if not os.path.exists('checkpoints/mono_boost/{}'.format(tag)):
            os.makedirs('checkpoints/mono_boost/{}'.format(tag))

        print(colored('Begin mono boosting... ', 'yellow'))
        if self.WINNER_NUM_PER_CASE:

            keys = ['checkpoint', 'mono_boost', dataset.dataset_name, 'deberta']

            if len(find_dirs(self.ROOT, keys)) < self.CLASSIFIER_TRAINING_NUM + 1:
                # _config.log_step = -1
                Trainer(config=_config,
                        dataset=dataset,  # train set and test set will be automatically detected
                        checkpoint_save_mode=1,
                        path_to_save='checkpoints/mono_boost/{}/'.format(tag),
                        auto_device=self.device  # automatic choose CUDA or CPU
                        )

            torch.cuda.empty_cache()
            time.sleep(5)

            checkpoint_path = ''
            max_f1 = ''
            for path in find_dirs(self.ROOT, keys):
                if 'f1' in path and path[path.index('f1'):] > max_f1:
                    max_f1 = max(path[path.index('f1'):], checkpoint_path)
                    checkpoint_path = path

            sent_classifier = TextClassifierCheckpointManager.get_text_classifier(checkpoint_path, auto_device=self.device)

            sent_classifier.opt.eval_batch_size = 128

            MLM, tokenizer = get_mlm_and_tokenizer(sent_classifier, _config)

            dataset_files = detect_dataset(dataset, task)
            boost_sets = dataset_files['train']
            augmentations = []
            perplexity_list = []
            confidence_list = []

            for boost_set in boost_sets:
                print('Augmenting -> {}'.format(boost_set))
                fin = open(boost_set, encoding='utf8', mode='r')
                lines = fin.readlines()
                fin.close()
                # remove(boost_set)
                for i in tqdm.tqdm(range(0, len(lines)), postfix='Augmenting...'):

                    lines[i] = lines[i].strip().replace('$LABEL$', 'PLACEHOLDER')
                    label = lines[i].split('PLACEHOLDER')[1].strip()

                    if self.AUGMENT_BACKEND in 'EDA':
                        raw_augs = self.augmenter.augment(lines[i])
                    else:
                        raw_augs = self.augmenter.augment(lines[i], n=self.AUGMENT_NUM_PER_CASE, num_thread=os.cpu_count())

                    if isinstance(raw_augs, str):
                        raw_augs = [raw_augs]
                    augs = {}
                    for text in raw_augs:
                        if text.endswith('PLACEHOLDER {}'.format(label)):
                            with torch.no_grad():
                                results = sent_classifier.infer(text.replace('PLACEHOLDER', '!ref!'), print_result=False)
                                ids = tokenizer(text.replace('PLACEHOLDER', '{}'.format(label)), return_tensors="pt")
                                ids['labels'] = ids['input_ids'].clone()
                                ids = ids.to(self.device)
                                loss = MLM(**ids)['loss']
                                perplexity = torch.exp(loss / ids['input_ids'].size(1))

                                perplexity_list.append(perplexity.item())
                                confidence_list.append(results[0]['confidence'])

                                if results[0]['ref_check'] == 'Correct' and results[0]['confidence'] > self.CONFIDENCE_THRESHOLD:
                                    augs[perplexity.item()] = [text.replace('PLACEHOLDER', '$LABEL$')]

                    key_rank = sorted(augs.keys())
                    for key in key_rank[:self.WINNER_NUM_PER_CASE]:
                        if key < self.PERPLEXITY_THRESHOLD:
                            augmentations += augs[key]

            print('Avg Confidence: {} Max Confidence: {} Min Confidence: {}'.format(np.average(confidence_list), max(confidence_list), min(confidence_list)))

            print('Avg Perplexity: {} Max Perplexity: {} Min Perplexity: {}'.format(np.average(perplexity_list), max(perplexity_list), min(perplexity_list)))

            fout = open('{}/{}.mono_boost.train.augment.ignore'.format(os.path.dirname(boost_set), tag), encoding='utf8', mode='w')

            for line in augmentations:
                fout.write(line + '\n')
            fout.close()

            del sent_classifier
            del MLM

            torch.cuda.empty_cache()
            time.sleep(5)

            post_clean(os.path.dirname(boost_set))

        for f in find_cwd_files('.ignore'):
            rename(f, f.replace('.ignore', ''))

        if train_after_aug:
            print(colored('Start mono boosting augment...', 'yellow'))
            return Trainer(config=config,
                           dataset=dataset,  # train set and test set will be automatically detected
                           checkpoint_save_mode=0,  # =None to avoid save model
                           auto_device=self.device  # automatic choose CUDA or CPU
                           )

    def apc_boost_free_training(self, config: APCConfigManager,
                                dataset: DatasetItem,
                                task='apc',
                                ):
        if not isinstance(dataset, DatasetItem) and os.path.exists(dataset):
            dataset = DatasetItem(dataset)
        prepare_dataset_and_clean_env(dataset.dataset_name, task, rewrite_cache=True)

        return Trainer(config=config,
                       dataset=dataset,  # train set and test set will be automatically detected
                       checkpoint_save_mode=0,  # =None to avoid save model
                       auto_device=self.device  # automatic choose CUDA or CPU
                       )

    def apc_classic_boost_training(self, config: APCConfigManager,
                                   dataset: DatasetItem,
                                   task='apc',
                                   rewrite_cache=True,
                                   train_after_aug=False
                                   ):
        if not isinstance(dataset, DatasetItem) and os.path.exists(dataset):
            dataset = DatasetItem(dataset)
        _config = self.get_apc_config(config)
        if 'pretrained_bert' in _config.args:
            tag = '{}_{}_{}'.format(_config.model.__name__.lower(), dataset.dataset_name, os.path.basename(_config.pretrained_bert))
        else:
            tag = '{}_{}_{}'.format(_config.model.__name__.lower(), dataset.dataset_name, 'glove')
        if rewrite_cache:
            prepare_dataset_and_clean_env(dataset.dataset_name, task, rewrite_cache)

        train_data = []
        for dataset_file in detect_dataset(dataset, task)['train']:
            print('processing {}'.format(dataset_file))
            fin = open(dataset_file, encoding='utf8', mode='r')
            lines = fin.readlines()
            fin.close()
            # rename(dataset_file, dataset_file + '.ignore')
            for i in tqdm.tqdm(range(0, len(lines), 3)):
                lines[i] = lines[i].strip()
                lines[i + 1] = lines[i + 1].strip()
                lines[i + 2] = lines[i + 2].strip()

                train_data.append([lines[i], lines[i + 1], lines[i + 2]])

        if self.WINNER_NUM_PER_CASE:

            fout_aug_train = open('{}/classic.train.{}.augment'.format(os.path.dirname(dataset_file), tag), encoding='utf8', mode='w')

            for item in tqdm.tqdm(train_data, postfix='Augmenting...'):

                item[0] = item[0].replace('$T$', 'PLACEHOLDER')

                if self.AUGMENT_BACKEND in 'EDA':
                    augs = self.augmenter.augment(item[0])
                else:
                    augs = self.augmenter.augment(item[0], n=self.AUGMENT_NUM_PER_CASE, num_thread=os.cpu_count())

                if isinstance(augs, str):
                    augs = [augs]
                for aug in augs:
                    if 'PLACEHOLDER' in aug:
                        _text = aug.replace('PLACEHOLDER', '$T$')
                        fout_aug_train.write(_text + '\n')
                        fout_aug_train.write(item[1] + '\n')
                        fout_aug_train.write(item[2] + '\n')

            fout_aug_train.close()

        post_clean(os.path.dirname(dataset_file))

        if train_after_aug:
            print(colored('Start classic augment training...', 'cyan'))
            return Trainer(config=config,
                           dataset=dataset,  # train set and test set will be automatically detected
                           auto_device=self.device  # automatic choose CUDA or CPU
                           ).load_trained_model()

    def apc_cross_boost_training(self, config: APCConfigManager,
                                 dataset: DatasetItem,
                                 rewrite_cache=True,
                                 task='apc',
                                 train_after_aug=False
                                 ):
        if not isinstance(dataset, DatasetItem) and os.path.exists(dataset):
            dataset = DatasetItem(dataset)
        _config = self.get_apc_config(config)
        if 'pretrained_bert' in _config.args:
            tag = '{}_{}_{}'.format(_config.model.__name__.lower(), dataset.dataset_name, os.path.basename(_config.pretrained_bert))
        else:
            tag = '{}_{}_{}'.format(_config.model.__name__.lower(), dataset.dataset_name, 'glove')

        prepare_dataset_and_clean_env(dataset.dataset_name, task, rewrite_cache)

        data_dict = query_dataset_detail(dataset_name=dataset, task='apc')

        for valid_file in detect_dataset(dataset, task)['valid']:
            rename(valid_file, valid_file + '.ignore')

        data = []
        dataset_file = ''
        dataset_files = detect_dataset(dataset, task)['train']

        for dataset_file in dataset_files:
            print('processing {}'.format(dataset_file))
            fin = open(dataset_file, encoding='utf8', mode='r')
            lines = fin.readlines()
            fin.close()
            rename(dataset_file, dataset_file + '.ignore')
            for i in tqdm.tqdm(range(0, len(lines), 3)):
                lines[i] = lines[i].strip()
                lines[i + 1] = lines[i + 1].strip()
                lines[i + 2] = lines[i + 2].strip()

                data.append([lines[i], lines[i + 1], lines[i + 2]])

        train_data = data
        len_per_fold = len(train_data) // self.BOOSTING_FOLD + 1
        folds = [train_data[i: i + len_per_fold] for i in range(0, len(train_data), len_per_fold)]

        if not os.path.exists('checkpoints/cross_boost/{}'.format(tag)):
            os.makedirs('checkpoints/cross_boost/{}'.format(tag))

        for fold_id, b_idx in enumerate(range(len(folds))):
            print(colored('boosting... No.{} in {} folds'.format(b_idx + 1, self.BOOSTING_FOLD), 'red'))
            f = find_file(self.ROOT, [tag, '{}.'.format(fold_id), '.augment.ignore'])
            if f:
                rename(f, f.replace('.ignore', ''))
                continue
            train_data = list(itertools.chain(*[x for i, x in enumerate(folds) if i != b_idx]))
            valid_data = folds[b_idx]

            fout_train = open('{}/train.dat.tmp'.format(os.path.dirname(dataset_file), fold_id), encoding='utf8', mode='w')
            fout_boost = open('{}/valid.dat.tmp'.format(os.path.dirname(dataset_file), fold_id), encoding='utf8', mode='w')
            for case in train_data:
                for line in case:
                    fout_train.write(line + '\n')

            for case in valid_data:
                for line in case:
                    fout_boost.write(line + '\n')

            fout_train.close()
            fout_boost.close()

            keys = ['checkpoint', 'cross_boost', dataset.dataset_name, 'fast_lcf_bert', 'deberta', 'No.{}'.format(b_idx + 1)]
            # keys = ['checkpoint', 'cross_boost', 'fast_lcf_bert', 'deberta', 'No.{}'.format(b_idx + 1)]

            if len(find_dirs(self.ROOT, keys)) < self.CLASSIFIER_TRAINING_NUM + 1:
                # _config.log_step = -1
                Trainer(config=_config,
                        dataset=dataset,  # train set and test set will be automatically detected
                        checkpoint_save_mode=1,
                        path_to_save='checkpoints/cross_boost/{}/No.{}/'.format(tag, b_idx + 1),
                        auto_device=self.device  # automatic choose CUDA or CPU
                        )

            torch.cuda.empty_cache()
            time.sleep(5)

            checkpoint_path = ''
            max_f1 = ''
            for path in find_dirs(self.ROOT, keys):
                if 'f1' in path and path[path.index('f1'):] > max_f1:
                    max_f1 = max(path[path.index('f1'):], checkpoint_path)
                    checkpoint_path = path

            sent_classifier = APCCheckpointManager.get_sentiment_classifier(checkpoint_path, auto_device=self.device)

            sent_classifier.opt.eval_batch_size = 128

            MLM, tokenizer = get_mlm_and_tokenizer(sent_classifier, _config)

            dataset_files = detect_dataset(dataset, task)
            boost_sets = dataset_files['valid']
            augmentations = []
            perplexity_list = []
            confidence_list = []

            aug_dict = {}
            for boost_set in boost_sets:
                if self.AUGMENT_NUM_PER_CASE <= 0:
                    continue
                print('Augmenting -> {}'.format(boost_set))
                fin = open(boost_set, encoding='utf8', mode='r')
                lines = fin.readlines()
                fin.close()
                remove(boost_set)
                for i in tqdm.tqdm(range(0, len(lines), 3), postfix='No.{} Augmenting...'.format(b_idx + 1)):

                    lines[i] = lines[i].strip().replace('$T$', 'PLACEHOLDER')
                    lines[i + 1] = lines[i + 1].strip()
                    lines[i + 2] = lines[i + 2].strip()

                    if self.AUGMENT_BACKEND in 'EDA':
                        raw_augs = self.augmenter.augment(lines[i])
                    else:
                        raw_augs = self.augmenter.augment(lines[i], n=self.AUGMENT_NUM_PER_CASE, num_thread=os.cpu_count())

                    if isinstance(raw_augs, str):
                        raw_augs = [raw_augs]
                    augs = {}
                    for text in raw_augs:
                        if 'PLACEHOLDER' in text:
                            _text = text.replace('PLACEHOLDER', '[ASP]{}[ASP] '.format(lines[i + 1])) + ' !sent! {}'.format(lines[i + 2])
                        else:
                            continue

                        with torch.no_grad():
                            results = sent_classifier.infer(_text, print_result=False)
                            ids = tokenizer(text.replace('PLACEHOLDER', '{}'.format(lines[i + 1])), return_tensors="pt")
                            ids['labels'] = ids['input_ids'].clone()
                            ids = ids.to(self.device)
                            loss = MLM(**ids)['loss']
                            perplexity = torch.exp(loss / ids['input_ids'].size(1))

                            perplexity_list.append(perplexity.item())
                            confidence_list.append(results[0]['confidence'][0])

                            if self.USE_LABEL:
                                if results[0]['ref_check'][0] != 'Correct':
                                    continue

                            if self.USE_CONFIDENCE:
                                if results[0]['confidence'][0] <= self.CONFIDENCE_THRESHOLD:
                                    continue
                            augs[perplexity.item()] = [text.replace('PLACEHOLDER', '$T$'), lines[i + 1], lines[i + 2]]

                    if self.USE_CONFIDENCE:
                        key_rank = sorted(augs.keys())
                    else:
                        key_rank = list(augs.keys())

                    for key in key_rank[:self.WINNER_NUM_PER_CASE]:
                        if self.USE_PERPLEXITY:
                            if key < self.PERPLEXITY_THRESHOLD:
                                augmentations += augs[key]
                        else:
                            augmentations += augs[key]

                            # d = aug_dict.get(results[0]['ref_sentiment'][0], [])
                            # d.append([text.replace('PLACEHOLDER', '$T$'), lines[i + 1], lines[i + 2]])
                            # aug_dict[results[0]['ref_sentiment'][0]] = d
            print('Avg Confidence: {} Max Confidence: {} Min Confidence: {}'.format(np.average(confidence_list), max(confidence_list), min(confidence_list)))
            print('Avg Perplexity: {} Max Perplexity: {} Min Perplexity: {}'.format(np.average(perplexity_list), max(perplexity_list), min(perplexity_list)))

            fout = open('{}/{}.cross_boost.{}.train.augment.ignore'.format(os.path.dirname(dataset_file), fold_id, tag), encoding='utf8', mode='w')
            #
            # min_num = min([len(d) for d in aug_dict.values()])
            # for key, value in aug_dict.items():
            #     # random.shuffle(value)
            #     augmentations += value[:int(len(value)*data_dict[key])]
            #
            # for aug in augmentations:
            #     for line in aug:
            #         fout.write(line + '\n')

            for line in augmentations:
                fout.write(line + '\n')
            fout.close()

            del sent_classifier
            del MLM

            torch.cuda.empty_cache()
            time.sleep(5)

            post_clean(os.path.dirname(dataset_file))

        for f in find_cwd_files('.ignore'):
            rename(f, f.replace('.ignore', ''))

        if train_after_aug:
            print(colored('Start cross boosting augment...', 'green'))
            return Trainer(config=config,
                           dataset=dataset,  # train set and test set will be automatically detected
                           checkpoint_save_mode=0,  # =None to avoid save model
                           auto_device=self.device  # automatic choose CUDA or CPU
                           )

    def apc_mono_boost_training(self, config: APCConfigManager,
                                dataset: DatasetItem,
                                rewrite_cache=True,
                                task='apc',
                                train_after_aug=False
                                ):
        if not isinstance(dataset, DatasetItem) and os.path.exists(dataset):
            dataset = DatasetItem(dataset)
        _config = self.get_apc_config(config)
        if 'pretrained_bert' in _config.args:
            tag = '{}_{}_{}'.format(_config.model.__name__.lower(), dataset.dataset_name, os.path.basename(_config.pretrained_bert))
        else:
            tag = '{}_{}_{}'.format(_config.model.__name__.lower(), dataset.dataset_name, 'glove')

        prepare_dataset_and_clean_env(dataset.dataset_name, task, rewrite_cache)

        if not os.path.exists('checkpoints/mono_boost/{}'.format(tag)):
            os.makedirs('checkpoints/mono_boost/{}'.format(tag))

        print(colored('Begin mono boosting... ', 'yellow'))
        if self.WINNER_NUM_PER_CASE:

            keys = ['checkpoint', 'mono_boost', 'fast_lcf_bert', dataset.dataset_name, 'deberta']

            if len(find_dirs(self.ROOT, keys)) < self.CLASSIFIER_TRAINING_NUM + 1:
                # _config.log_step = -1
                Trainer(config=_config,
                        dataset=dataset,  # train set and test set will be automatically detected
                        checkpoint_save_mode=1,
                        path_to_save='checkpoints/mono_boost/{}/'.format(tag),
                        auto_device=self.device  # automatic choose CUDA or CPU
                        )

            torch.cuda.empty_cache()
            time.sleep(5)

            checkpoint_path = ''
            max_f1 = ''
            for path in find_dirs(self.ROOT, keys):
                if 'f1' in path and path[path.index('f1'):] > max_f1:
                    max_f1 = max(path[path.index('f1'):], checkpoint_path)
                    checkpoint_path = path

            sent_classifier = APCCheckpointManager.get_sentiment_classifier(checkpoint_path, auto_device=self.device)

            sent_classifier.opt.eval_batch_size = 128

            MLM, tokenizer = get_mlm_and_tokenizer(sent_classifier, _config)

            dataset_files = detect_dataset(dataset, task)
            boost_sets = dataset_files['train']
            augmentations = []
            perplexity_list = []
            confidence_list = []

            for boost_set in boost_sets:
                print('Augmenting -> {}'.format(boost_set))
                fin = open(boost_set, encoding='utf8', mode='r')
                lines = fin.readlines()
                fin.close()
                for i in tqdm.tqdm(range(0, len(lines), 3), postfix='Mono Augmenting...'):

                    lines[i] = lines[i].strip().replace('$T$', 'PLACEHOLDER')
                    lines[i + 1] = lines[i + 1].strip()
                    lines[i + 2] = lines[i + 2].strip()

                    if self.AUGMENT_BACKEND in 'EDA':
                        raw_augs = self.augmenter.augment(lines[i])
                    else:
                        raw_augs = self.augmenter.augment(lines[i], n=self.AUGMENT_NUM_PER_CASE, num_thread=os.cpu_count())

                    if isinstance(raw_augs, str):
                        raw_augs = [raw_augs]
                    augs = {}
                    for text in raw_augs:
                        if 'PLACEHOLDER' in text:
                            _text = text.replace('PLACEHOLDER', '[ASP]{}[ASP] '.format(lines[i + 1])) + ' !sent! {}'.format(lines[i + 2])
                        else:
                            continue

                        with torch.no_grad():
                            results = sent_classifier.infer(_text, print_result=False)
                            ids = tokenizer(text.replace('PLACEHOLDER', '{}'.format(lines[i + 1])), return_tensors="pt")
                            ids['labels'] = ids['input_ids'].clone()
                            ids = ids.to(self.device)
                            loss = MLM(**ids)['loss']
                            perplexity = torch.exp(loss / ids['input_ids'].size(1))

                            perplexity_list.append(perplexity.item())
                            confidence_list.append(results[0]['confidence'][0])

                            if results[0]['ref_check'][0] == 'Correct' and results[0]['confidence'][0] > self.CONFIDENCE_THRESHOLD:
                                augs[perplexity.item()] = [text.replace('PLACEHOLDER', '$T$'), lines[i + 1], lines[i + 2]]

                    key_rank = sorted(augs.keys())
                    for key in key_rank[:self.WINNER_NUM_PER_CASE]:
                        if key < self.PERPLEXITY_THRESHOLD:
                            augmentations += augs[key]

            print('Avg Confidence: {} Max Confidence: {} Min Confidence: {}'.format(np.average(confidence_list), max(confidence_list), min(confidence_list)))
            print('Avg Perplexity: {} Max Perplexity: {} Min Perplexity: {}'.format(np.average(perplexity_list), max(perplexity_list), min(perplexity_list)))

            fout = open('{}/{}.mono_boost.train.augment'.format(os.path.dirname(boost_set), tag), encoding='utf8', mode='w')

            for line in augmentations:
                fout.write(line + '\n')
            fout.close()

            del sent_classifier
            del MLM

            torch.cuda.empty_cache()
            time.sleep(5)

            post_clean(os.path.dirname(boost_set))

        for f in find_cwd_files('.ignore'):
            rename(f, f.replace('.ignore', ''))

        if train_after_aug:
            print(colored('Start mono boosting augment...', 'yellow'))
            return Trainer(config=config,
                           dataset=dataset,  # train set and test set will be automatically detected
                           checkpoint_save_mode=0,  # =None to avoid save model
                           auto_device=self.device  # automatic choose CUDA or CPU
                           )


def query_dataset_detail(dataset_name, task='text_classification'):
    dataset_files = detect_dataset(dataset_name, task)
    data_dict = {}
    data_sum = 0
    if task in 'text_classification':
        for train_file in dataset_files['train']:
            with open(train_file, mode='r', encoding='utf8') as fin:
                lines = fin.readlines()
                for i in range(0, len(lines), 0):
                    data_dict[lines[i].strip()] = data_dict.get(lines[i].split('$LABEL$')[-1].strip(), 0) + 1
                    data_sum += 1
    else:
        for train_file in dataset_files['train']:
            with open(train_file, mode='r', encoding='utf8') as fin:
                lines = fin.readlines()
                for i in range(0, len(lines), 3):
                    data_dict[lines[i + 2].strip()] = data_dict.get(lines[i + 2].strip(), 0) + 1
                    data_sum += 1

    for label in data_dict:
        data_dict[label] = 1 - (data_dict[label] / data_sum)
    return data_dict


def post_clean(dataset_path):
    if os.path.exists('{}/train.dat.tmp'.format(os.path.dirname(dataset_path))):
        remove('{}/train.dat.tmp'.format(os.path.dirname(dataset_path)))
    if os.path.exists('{}/valid.dat.tmp'.format(os.path.dirname(dataset_path))):
        remove('{}/valid.dat.tmp'.format(os.path.dirname(dataset_path)))

    for f in find_cwd_files('.tmp'):
        remove(f)

    if find_cwd_dir('run'):
        shutil.rmtree(find_cwd_dir('run'))


def prepare_dataset_and_clean_env(dataset, task, rewrite_cache=False):
    # download_datasets_from_github('..')
    # backup_datasets_dir = find_dir('../integrated_datasets', [dataset, task], disable_alert=True, recursive=True)
    #
    # datasets_dir = find_dir('.', ['integrated_datasets', dataset, task], disable_alert=True)
    # if not datasets_dir and backup_datasets_dir:
    #     datasets_dir = backup_datasets_dir[backup_datasets_dir.find('integrated_datasets'):]
    #     os.makedirs(datasets_dir)

    download_datasets_from_github('.')
    datasets_dir = 'integrated_datasets'
    if rewrite_cache:
        print('Remove temp files (if any)')
        for f in find_files(datasets_dir, ['.augment']) + find_files(datasets_dir, ['.tmp']) + find_files(datasets_dir, ['.ignore']):
            # for f in find_files(datasets_dir, ['.tmp']):
            remove(f)
        os.system('rm {}/valid.dat.tmp'.format(datasets_dir))
        os.system('rm {}/train.dat.tmp'.format(datasets_dir))
        if find_cwd_dir(['run', dataset]):
            shutil.rmtree(find_cwd_dir(['run', dataset]))

        print('Remove Done')

    # for f in os.listdir(backup_datasets_dir):
    #     if os.path.isfile(os.path.join(backup_datasets_dir, f)):
    #         shutil.copyfile(os.path.join(backup_datasets_dir, f), os.path.join(datasets_dir, f))
    #     if os.path.isdir(os.path.join(backup_datasets_dir, f)):
    #         shutil.copytree(os.path.join(backup_datasets_dir, f), os.path.join(datasets_dir, f))


filter_key_words = ['.py', '.ignore', '.md', 'readme', 'log', 'result', 'zip', '.state_dict', '.model', '.png', 'acc_', 'f1_', '.aug']


def detect_dataset(dataset_path, task='apc'):
    if not isinstance(dataset_path, DatasetItem):
        dataset_path = DatasetItem(dataset_path)
    dataset_file = {'train': [], 'test': [], 'valid': []}
    search_path = ''
    d = ''
    for d in dataset_path:
        if not os.path.exists(d) or hasattr(ABSADatasetList, d) or hasattr(ClassificationDatasetList, d):
            print('{} dataset is loading from: {}'.format(d, 'https://github.com/yangheng95/ABSADatasets'))
            download_datasets_from_github(os.getcwd())
            search_path = find_dir(os.getcwd(), [d, task, 'dataset'], exclude_key=['infer', 'test.'] + filter_key_words, disable_alert=False)
            dataset_file['train'] += find_files(search_path, [d, 'train', task], exclude_key=['.inference', 'test.'] + filter_key_words)
            dataset_file['test'] += find_files(search_path, [d, 'test', task], exclude_key=['inference', 'train.'] + filter_key_words)
            dataset_file['valid'] += find_files(search_path, [d, 'valid', task], exclude_key=['inference', 'train.'] + filter_key_words)
            dataset_file['valid'] += find_files(search_path, [d, 'dev', task], exclude_key=['inference', 'train.'] + filter_key_words)
        else:
            dataset_file['train'] = find_files(d, ['train', task], exclude_key=['.inference', 'test.'] + filter_key_words)
            dataset_file['test'] = find_files(d, ['test', task], exclude_key=['.inference', 'train.'] + filter_key_words)
            dataset_file['valid'] = find_files(d, ['valid', task], exclude_key=['.inference', 'train.'] + filter_key_words)
            dataset_file['valid'] += find_files(d, ['dev', task], exclude_key=['inference', 'train.'] + filter_key_words)

    if len(dataset_file['train']) == 0:
        if os.path.isdir(max(d, search_path)):
            print('No train set found from: {}, unrecognized files: {}'.format(dataset_path, ', '.join(os.listdir(max(d, search_path)))))
        raise RuntimeError('Fail to locate dataset: {}. If you are using your own dataset,' ' you may need rename your dataset according to {}'.format(
            dataset_path, 'https://github.com/yangheng95/ABSADatasets#important-rename-your-dataset-filename-before-use-it-in-pyabsa'))

    if len(dataset_file['test']) == 0:
        print('Warning, auto_evaluate=True, however cannot find test set using for evaluating!')

    if len(dataset_path) > 1:
        print(colored('Never mixing datasets with different sentiment labels for training & inference !', 'yellow'))

    return dataset_file