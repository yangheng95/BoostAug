# -*- coding: utf-8 -*-
# file: train_text_classification_bert.py
# time: 2021/8/5
# author: yangheng <yangheng@m.scnu.edu.cn>
# github: https://github.com/yangheng95
# Copyright (C) 2021. All Rights Reserved.
import os
import warnings

import findfile

from pyabsa import TextClassificationTrainer, ClassificationConfigManager, ClassificationDatasetList
from pyabsa.functional import BERTClassificationModelList
from pyabsa.functional.dataset import DatasetItem

warnings.filterwarnings('ignore')

classification_config_english = ClassificationConfigManager.get_classification_config_english()
classification_config_english.model = BERTClassificationModelList.BERT
classification_config_english.num_epoch = 10
classification_config_english.patience = 3
# classification_config_english.pretrained_bert = 'checkpoints/bert_SST_acc_94.95_f1_94.95/fine-tuned-pretrained-model'
classification_config_english.evaluate_begin = 0
classification_config_english.max_seq_len = 80
classification_config_english.log_step = -1
classification_config_english.dropout = 0.5
classification_config_english.learning_rate = 1e-5
classification_config_english.cache_dataset = True
classification_config_english.seed = {12}
classification_config_english.l2reg = 1e-8
classification_config_english.cross_validate_fold = -1

for f in findfile.find_cwd_files('.augment.ignore'):
    os.rename(f, f.replace('.augment.ignore', '.augment'))

dataset = ClassificationDatasetList.SST
text_classifier = TextClassificationTrainer(config=classification_config_english,
                                            dataset=dataset,
                                            checkpoint_save_mode=3,
                                            auto_device=True
                                            ).load_trained_model()
