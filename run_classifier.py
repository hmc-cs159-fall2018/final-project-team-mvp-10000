# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HugginFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""BERT finetuning runner."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import csv
import os
import logging
import argparse
import random
import math
import multiprocessing
from itertools import count, repeat, islice
from functools import reduce
from lxml import etree
from tqdm import tqdm, trange
from html import unescape

import preprocess
import predict
import numpy as np
import torch
from torch.utils.data import TensorDataset, DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler

from pytorch_pretrained_bert.tokenization import BertTokenizer
from pytorch_pretrained_bert.modeling import BertForSequenceClassification
from pytorch_pretrained_bert.optimization import BertAdam
from pytorch_pretrained_bert.file_utils import PYTORCH_PRETRAINED_BERT_CACHE
from bertaverager import BertForSplicedSequenceClassification

logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s', 
                    datefmt = '%m/%d/%Y %H:%M:%S',
                    level = logging.INFO)
logger = logging.getLogger(__name__)


class InputExample(object):
    """A single training/test example for simple sequence classification."""

    def __init__(self, guid, text_a, text_b=None, label=None):
        """Constructs a InputExample.

        Args:
            guid: Unique id for the example.
            text_a: string. The untokenized text of the first sequence. For single
            sequence tasks, only this sequence must be specified.
            text_b: (Optional) string. The untokenized text of the second sequence.
            Only must be specified for sequence pair tasks.
            label: (Optional) string. The label of the example. This should be
            specified for train and dev examples, but not for test examples.
        """
        self.guid = guid
        self.text_a = text_a
        self.text_b = text_b
        self.label = label


class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self, input_ids, input_mask, segment_ids, label_id, article_id=None):
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.label_id = label_id
        self.article_id = article_id


class DataProcessor(object):
    """Base class for data converters for sequence classification data sets."""

    def get_train_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the train set."""
        raise NotImplementedError()

    def get_dev_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the dev set."""
        raise NotImplementedError()

    def get_labels(self):
        """Gets the list of labels for this data set."""
        raise NotImplementedError()

    @classmethod
    def _read_tsv(cls, input_file, quotechar=None):
        """Reads a tab separated value file."""
        with open(input_file, "r") as f:
            reader = csv.reader(f, delimiter="\t", quotechar=quotechar)
            lines = []
            for line in reader:
                lines.append(line)
            return lines


class MrpcProcessor(DataProcessor):
    """Processor for the MRPC data set (GLUE version)."""

    def get_train_examples(self, data_dir):
        """See base class."""
        logger.info("LOOKING AT {}".format(os.path.join(data_dir, "train.tsv")))
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "train.tsv")), "train")

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "dev.tsv")), "dev")

    def get_labels(self):
        """See base class."""
        return ["0", "1"]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            guid = "%s-%s" % (set_type, i)
            text_a = line[3]
            text_b = line[4]
            label = line[0]
            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))
        return examples


class MnliProcessor(DataProcessor):
    """Processor for the MultiNLI data set (GLUE version)."""

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "train.tsv")), "train")

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "dev_matched.tsv")),
            "dev_matched")

    def get_labels(self):
        """See base class."""
        return ["contradiction", "entailment", "neutral"]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            guid = "%s-%s" % (set_type, line[0])
            text_a = line[8]
            text_b = line[9]
            label = line[-1]
            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))
        return examples


class ColaProcessor(DataProcessor):
    """Processor for the CoLA data set (GLUE version)."""

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "train.tsv")), "train")

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "dev.tsv")), "dev")

    def get_labels(self):
        """See base class."""
        return ["0", "1"]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            guid = "%s-%s" % (set_type, i)
            text_a = line[3]
            label = line[1]
            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
        return examples

class SemevalOfficialProcessor(DataProcessor):
    def __init__(self):
        super().__init__()
        self.examples = None

    """Processor for providing model with examples for inference during official competition"""
    def get_train_examples(self, data_dir):
        raise NotImplementedError()

    def get_dev_examples(self, data_dir):
        for f in os.listdir(data_dir):
            if f.endswith(".xml"):
                return self._create_examples(open(data_dir + "/" + f, "rb"), data_dir, "inference")

    def get_labels(self):
        return ["false", "true"]

    def _create_examples(self, data_file, data_dir, set_type):
        examples = []

        temp_dir = data_dir.rstrip("/") + "_preprocessed"
        if not(os.path.exists(temp_dir)): 
            os.mkdir(temp_dir)
        temp_fname = os.path.join(temp_dir, "articles.xml")
        temp_fp = open(temp_fname, "wb")
        temp_fp = predict.do_preprocess([data_file], temp_fp)

        for index, article in enumerate(self._do_xml_parse(temp_fp, 'article')):
            article_id = article.get('id')
            article_guid = "%s-%s" % (set_type, article_id)
            article_text = " ".join(self._extract_text(article).split())
            examples.append(InputExample(guid=article_guid, text_a=article_text, text_b=None, label=None))
        return examples

    def _do_xml_parse(self, fp, tag, max_elements=None, progress_message=None):
        print(type)
        fp.seek(0)
        elements = enumerate(islice(etree.iterparse(fp, tag=tag), max_elements))
        for i, (event, elem) in elements:
            yield elem   # Returns the current element.
            elem.clear() # Empties out the list contained in elem to save memory.
            if progress_message and (i % 1000 == 0):
                print(progress_message.format(i), file=sys.stderr, end='\r')
        if progress_message: print(file=sys.stderr)

    def _extract_text(self, article):
        return unescape("".join([x for x in article.find("spacy").itertext()]).lower())

class SemevalProcessor(DataProcessor):
    def __init__(self):
        super().__init__()
        self.examples = None
    
    """Processor for the Semeval data set."""
    def get_train_examples(self, data_dir):
        if self.examples is not None:
            return self.examples[:math.floor(len(self.examples))]
        else:
            self.examples = self._create_examples(data_dir)
            return self.examples[:math.floor(0.8*len(self.examples))]

    def get_dev_examples(self, data_dir):
        if self.examples is not None:
            return self.examples[math.floor(0.8*len(self.examples)):]
        else:
            self.examples = self._create_examples(data_dir)
            return self.examples[math.floor(0.8*len(self.examples)):]

    def get_labels(self):
        return ["false", "true"]

    def _create_examples(self, data_dir):
        train_directory = os.path.join(data_dir, "training/preprocessed")
        data_file = open(os.path.join(train_directory, "articles-training-byarticle-20181122.prep.txt"), "r")
        label_file = open(os.path.join(train_directory, "ground-truth-training-byarticle-20181122.txt"), "r")

        examples = []
        for i, (text_line, label) in enumerate(zip(data_file, label_file)):
            guid = str(i)
            text_a = text_line.strip()
            label = label.strip()
            examples.append(InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
   
        data_file.close()
        label_file.close()
        
        return examples

class SemevalProcessor2(DataProcessor):
    def __init__(self):
        super().__init__()
        self.examples = None
    
    """Alternate processor for the Semeval data set."""
    def get_train_examples(self, data_dir):
        train_directory = os.path.join(data_dir, "training/preprocessed")
        data_file = os.path.join(train_directory, "articles-training-byarticle-20181122.prep.txt")
        label_file = os.path.join(train_directory, "ground-truth-training-byarticle-20181122.txt")
        return self._create_examples(data_file, label_file, "train")
        

    def get_dev_examples(self, data_dir):
        validation_directory = os.path.join(data_dir, "validation/preprocessed")
        data_file = os.path.join(validation_directory, "articles-validation.prep.txt")
        label_file = os.path.join(validation_directory, "ground-truth-validation.txt")
        return self._create_examples(data_file, label_file, "validation")

    def get_labels(self):
        return ["false", "true"]

    def _create_examples(self, data_file, label_file, set_type):
        data_file = open(data_file, "r")
        label_file = open(label_file, "r")

        examples = []
        p = multiprocessing.Pool(multiprocessing.cpu_count())

        for example in tqdm(p.imap(create_example_semeval2, zip(count(), data_file, label_file, repeat(set_type)), chunksize=100), desc="Example Creation"):
            examples.append(example)
   
        data_file.close()
        label_file.close()

        return examples

def create_example_semeval2(inputs):
    i, text_line, label, set_type = inputs
    guid = "%s-%s" % (set_type, i)
    text_a = text_line.strip()
    label = label.strip()
    return InputExample(guid=guid, text_a=text_a, text_b=None, label=label)

def permutation(tokens, permute_ngrams):
    if permute_ngrams is None:
        return tokens

    ngrams = []
    i = 0
    num_tokens = len(tokens)

    while i < num_tokens:
        ngrams.append(tokens[i:i+permute_ngrams])
        i += permute_ngrams

    random.shuffle(ngrams)
    return reduce(lambda a,b: a + b, ngrams, [])


def construct_features(inputs):
    ex_index, example, max_seq_length, tokenizer, label_map, predict, permute_ngrams = inputs
    
    tokens_a = tokenizer.tokenize(example.text_a)
    tokens_a = permutation(tokens_a, permute_ngrams)

    tokens_b = None
    if example.text_b:
        tokens_b = tokenizer.tokenize(example.text_b)
        tokens_b = permutation(tokens_b, permute_ngrams)

    if tokens_b:
        # Modifies `tokens_a` and `tokens_b` in place so that the total
        # length is less than the specified length.
        # Account for [CLS], [SEP], [SEP] with "- 3"
        _truncate_seq_pair(tokens_a, tokens_b, max_seq_length - 3)
    else:
        # Account for [CLS] and [SEP] with "- 2"
        if len(tokens_a) > max_seq_length - 2:
            tokens_a = tokens_a[0:(max_seq_length - 2)]

    # The convention in BERT is:
    # (a) For sequence pairs:
    #  tokens:   [CLS] is this jack ##son ##ville ? [SEP] no it is not . [SEP]
    #  type_ids: 0   0  0    0    0     0       0 0    1  1  1  1   1 1
    # (b) For single sequences:
    #  tokens:   [CLS] the dog is hairy . [SEP]
    #  type_ids: 0   0   0   0  0     0 0
    #
    # Where "type_ids" are used to indicate whether this is the first
    # sequence or the second sequence. The embedding vectors for `type=0` and
    # `type=1` were learned during pre-training and are added to the wordpiece
    # embedding vector (and position vector). This is not *strictly* necessary
    # since the [SEP] token unambigiously separates the sequences, but it makes
    # it easier for the model to learn the concept of sequences.
    #
    # For classification tasks, the first vector (corresponding to [CLS]) is
    # used as as the "sentence vector". Note that this only makes sense because
    # the entire model is fine-tuned.
    tokens = []
    segment_ids = []
    tokens.append("[CLS]")
    segment_ids.append(0)
    for token in tokens_a:
        tokens.append(token)
        segment_ids.append(0)
    tokens.append("[SEP]")
    segment_ids.append(0)

    if tokens_b:
        for token in tokens_b:
            tokens.append(token)
            segment_ids.append(1)
        tokens.append("[SEP]")
        segment_ids.append(1)

    input_ids = tokenizer.convert_tokens_to_ids(tokens)

    # The mask has 1 for real tokens and 0 for padding tokens. Only real
    # tokens are attended to.
    input_mask = [1] * len(input_ids)

    # Zero-pad up to the sequence length.
    while len(input_ids) < max_seq_length:
        input_ids.append(0)
        input_mask.append(0)
        segment_ids.append(0)

    assert len(input_ids) == max_seq_length
    assert len(input_mask) == max_seq_length
    assert len(segment_ids) == max_seq_length

    if not predict:
        label_id = label_map[example.label]
    else: 
        label_id = None

    if ex_index < 1:
        logger.info("*** Example ***")
        logger.info("guid: %s" % (example.guid))
        logger.info("tokens: %s" % " ".join([str(x) for x in tokens]))
        logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
        logger.info("input_mask: %s" % " ".join([str(x) for x in input_mask]))
        logger.info("segment_ids: %s" % " ".join([str(x) for x in segment_ids]))
        if not predict: 
            logger.info("label: %s (id = %d)" % (example.label, label_id))
        else:
            logger.info("id: %s" % label_id)

    if not predict:
        return InputFeatures(input_ids=input_ids, input_mask=input_mask, segment_ids=segment_ids, label_id=label_id)
    else:
        return InputFeatures(input_ids=input_ids, input_mask=input_mask, segment_ids=segment_ids, label_id=label_id, article_id=(example.guid.split('-')[1]))


def convert_examples_to_features(examples, label_list, max_seq_length, tokenizer, predict=False, permute_ngrams=None):
    """Loads a data file into a list of `InputBatch`s."""
    label_map = {}
    for (i, label) in enumerate(label_list):
        label_map[label] = i

    features = [] 
    p = multiprocessing.Pool(multiprocessing.cpu_count())

    for feature in tqdm(p.imap(construct_features, 
                               zip(count(), examples, repeat(max_seq_length), repeat(tokenizer), repeat(label_map), 
                                   repeat(predict), repeat(permute_ngrams)), chunksize=100), 
                        desc="Example Creation"):
        features.append(feature)

    return features


def _truncate_seq_pair(tokens_a, tokens_b, max_length):
    """Truncates a sequence pair in place to the maximum length."""

    # This is a simple heuristic which will always truncate the longer sequence
    # one token at a time. This makes more sense than truncating an equal percent
    # of tokens from each, since if one sequence is very short then each token
    # that's truncated likely contains more information than a longer sequence.
    while True:
        total_length = len(tokens_a) + len(tokens_b)
        if total_length <= max_length:
            break
        if len(tokens_a) > len(tokens_b):
            tokens_a.pop()
        else:
            tokens_b.pop()

def copy_optimizer_params_to_model(named_params_model, named_params_optimizer):
    """ Utility function for optimize_on_cpu and 16-bits training.
        Copy the parameters optimized on CPU/RAM back to the model on GPU
    """
    for (name_opti, param_opti), (name_model, param_model) in zip(named_params_optimizer, named_params_model):
        if name_opti != name_model:
            logger.error("name_opti != name_model: {} {}".format(name_opti, name_model))
            raise ValueError
        param_model.data.copy_(param_opti.data)

def set_optimizer_params_grad(named_params_optimizer, named_params_model, test_nan=False):
    """ Utility function for optimize_on_cpu and 16-bits training.
        Copy the gradient of the GPU parameters to the CPU/RAMM copy of the model
    """
    is_nan = False
    for (name_opti, param_opti), (name_model, param_model) in zip(named_params_optimizer, named_params_model):
        if name_opti != name_model:
            logger.error("name_opti != name_model: {} {}".format(name_opti, name_model))
            raise ValueError
        if param_model.grad is not None:
            if test_nan and torch.isnan(param_model.grad).sum() > 0:
                is_nan = True
            if param_opti.grad is None:
                param_opti.grad = torch.nn.Parameter(param_opti.data.new().resize_(*param_opti.data.size()))
            param_opti.grad.data.copy_(param_model.grad.data)
        else:
            param_opti.grad = None
    return is_nan

def compute_validation_accuracy(model, eval_dataloader, device):
    model.eval()
    eval_accuracy = 0.0
    nb_eval_examples = 0

    for input_ids, input_mask, segment_ids, label_ids in tqdm(eval_dataloader, desc="Evaluation"):
        input_ids = input_ids.to(device)
        input_mask = input_mask.to(device)
        segment_ids = segment_ids.to(device)
        label_ids = label_ids.to(device)

        with torch.no_grad():
            logits = model(input_ids, segment_ids, input_mask)

        eval_accuracy += torch.sum(torch.argmax(logits, dim=1) == label_ids).item()
        nb_eval_examples += input_ids.size(0)

    return eval_accuracy / nb_eval_examples

def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--data_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The input data dir. Should contain the .tsv files (or other data files) for the task.")
    parser.add_argument("--bert_model", default=None, type=str, required=True,
                        help="Bert pre-trained model selected in the list: bert-base-uncased, "
                             "bert-large-uncased, bert-base-cased, bert-base-multilingual, bert-base-chinese.")
    parser.add_argument("--task_name",
                        default=None,
                        type=str,
                        required=True,
                        help="The name of the task to train.")
    parser.add_argument("--output_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The output directory where the model checkpoints will be written.")

    ## Other parameters
    parser.add_argument("--max_seq_length",
                        default=100,
                        type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. \n"
                             "Sequences longer than this will be truncated, and sequences shorter \n"
                             "than this will be padded.")
    parser.add_argument("--permute_ngrams",
                        default=None,
                        type=int,
                        help="At what granularity to permute the training articles. By default articles will not be permuted. The value n corresponds "
                             " to the size of the n-grams to permute.")
    parser.add_argument("--do_train",
                        default=False,
                        action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval",
                        default=False,
                        action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--do_lower_case",
                        default=False,
                        action='store_true',
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--train_batch_size",
                        default=32,
                        type=int,
                        help="Total batch size for training.")
    parser.add_argument("--eval_batch_size",
                        default=8,
                        type=int,
                        help="Total batch size for eval.")
    parser.add_argument("--learning_rate",
                        default=5e-5,
                        type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs",
                        default=3.0,
                        type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--warmup_proportion",
                        default=0.1,
                        type=float,
                        help="Proportion of training to perform linear learning rate warmup for. "
                             "E.g., 0.1 = 10%% of training.")
    parser.add_argument("--no_cuda",
                        default=False,
                        action='store_true',
                        help="Whether not to use CUDA when available")
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument('--seed', 
                        type=int, 
                        default=42,
                        help="random seed for initialization")
    parser.add_argument('--gradient_accumulation_steps',
                        type=int,
                        default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")                       
    parser.add_argument('--optimize_on_cpu',
                        default=False,
                        action='store_true',
                        help="Whether to perform optimization and keep the optimizer averages on CPU")
    parser.add_argument('--fp16',
                        default=False,
                        action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument('--loss_scale',
                        type=float, default=128,
                        help='Loss scaling, positive power of 2 values can improve fp16 convergence.')
    parser.add_argument('--model_path',
                        default=None,
                        help='Model path if you want to use a previously saved model.')
    parser.add_argument('--predict',
                        default=False,
                        action='store_true',
                        help='Flag determines if we are running this on TIRA.') 
    parser.add_argument('--model_no_save',
                        default=False,
                        action='store_true',
                        help='Flag ensures no models are saved so that computer storage does not fill. This flag is primarily useful for hyperparameter grid search.')

    args = parser.parse_args()

    processors = {
        "cola": ColaProcessor,
        "mnli": MnliProcessor,
        "mrpc": MrpcProcessor,
        "semeval": SemevalProcessor,
        "semeval2": SemevalProcessor2,
        "semevalofficial": SemevalOfficialProcessor,
    }

    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        n_gpu = torch.cuda.device_count()
    else:
        device = torch.device("cuda", args.local_rank)
        n_gpu = 1
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.distributed.init_process_group(backend='nccl')
        if args.fp16:
            logger.info("16-bits training currently not supported in distributed training")
            args.fp16 = False # (see https://github.com/pytorch/pytorch/pull/13496)
    logger.info("device %s n_gpu %d distributed training %r", device, n_gpu, bool(args.local_rank != -1))

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
                            args.gradient_accumulation_steps))

    args.train_batch_size = int(args.train_batch_size / args.gradient_accumulation_steps)

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not args.do_train and not args.do_eval:
        raise ValueError("At least one of `do_train` or `do_eval` must be True.")

    if args.do_train and args.do_eval:
        raise ValueError("`do_train` and `do_eval` together does not make much sense as training logs validation accuracy anyway.")

    if os.path.exists(args.output_dir) and os.listdir(args.output_dir):
        raise ValueError("Output directory ({}) already exists and is not empty.".format(args.output_dir))
    os.makedirs(args.output_dir, exist_ok=True)

    task_name = args.task_name.lower()

    if task_name not in processors:
        raise ValueError("Task not found: %s" % (task_name))

    processor = processors[task_name]()
    label_list = processor.get_labels()

    tokenizer = BertTokenizer.from_pretrained(args.bert_model, do_lower_case=args.do_lower_case)

    train_examples = None
    num_train_steps = None
    if args.do_train:
        train_examples = processor.get_train_examples(args.data_dir)
        num_train_steps = int(len(train_examples) / args.train_batch_size / args.gradient_accumulation_steps * args.num_train_epochs)

    # Prepare model
    model = BertForSequenceClassification.from_pretrained(args.bert_model, cache_dir=PYTORCH_PRETRAINED_BERT_CACHE / 'distributed_{}'.format(args.local_rank))
    
    if args.model_path is not None:
        model.load_state_dict(torch.load(args.model_path), strict=False)

    if args.fp16:
        model.half()
    model.to(device)
    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank],
                                                          output_device=args.local_rank)
    elif n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Prepare optimizer
    if args.fp16:
        param_optimizer = [(n, param.clone().detach().to('cpu').float().requires_grad_()) \
                            for n, param in model.named_parameters()]
    elif args.optimize_on_cpu:
        param_optimizer = [(n, param.clone().detach().to('cpu').requires_grad_()) \
                            for n, param in model.named_parameters()]
    else:
        param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'gamma', 'beta']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay_rate': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay_rate': 0.0}
        ]
    t_total = num_train_steps
    if args.local_rank != -1:
        t_total = t_total // torch.distributed.get_world_size()
    optimizer = BertAdam(optimizer_grouped_parameters,
                         lr=args.learning_rate,
                         warmup=args.warmup_proportion,
                         t_total=t_total)

    # In both training and evaluation you will use the validation dataset.
    eval_examples = processor.get_dev_examples(args.data_dir)
    eval_features = convert_examples_to_features(eval_examples, label_list, args.max_seq_length, tokenizer, 
                                                 args.predict, permute_ngrams=args.permute_ngrams)

    all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
    all_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
    all_segment_ids = torch.tensor([f.segment_ids for f in eval_features], dtype=torch.long)

    if not args.predict:    
        all_label_ids = torch.tensor([f.label_id for f in eval_features], dtype=torch.long)
        eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids)
    else:
        all_article_ids = torch.tensor([int(f.article_id) for f in eval_features], dtype=torch.long)
        eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_article_ids)

    eval_sampler = SequentialSampler(eval_data)
    eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)

    if args.do_train:
        train_features = convert_examples_to_features(train_examples, label_list, args.max_seq_length, tokenizer, permute_ngrams=args.permute_ngrams)
        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", len(train_examples))
        logger.info("  Batch size = %d", args.train_batch_size)
        all_input_ids = torch.tensor([f.input_ids for f in train_features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in train_features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in train_features], dtype=torch.long)
        all_label_ids = torch.tensor([f.label_id for f in train_features], dtype=torch.long)
        train_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids)
        if args.local_rank == -1:
            train_sampler = RandomSampler(train_data)
        else:
            train_sampler = DistributedSampler(train_data)
        train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=args.train_batch_size)
        output_eval_file = open(os.path.join(args.output_dir, "eval_results.txt"), "w")

        for i in trange(int(args.num_train_epochs), desc="Epoch"):

            model.train()
            for step, batch in enumerate(tqdm(train_dataloader, desc="Iteration")):
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_mask, segment_ids, label_ids = batch
                loss = model(input_ids, segment_ids, input_mask, label_ids)

                if n_gpu > 1:
                    loss = loss.mean() # mean() to average on multi-gpu.
                if args.fp16 and args.loss_scale != 1.0:
                    # rescale loss for fp16 training
                    # see https://docs.nvidia.com/deeplearning/sdk/mixed-precision-training/index.html
                    loss = loss * args.loss_scale
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps
                loss.backward()

                if (step + 1) % args.gradient_accumulation_steps == 0:
                    if args.fp16 or args.optimize_on_cpu:
                        if args.fp16 and args.loss_scale != 1.0:
                            # scale down gradients for fp16 training
                            for param in model.parameters():
                                if param.grad is not None:
                                    param.grad.data = param.grad.data / args.loss_scale
                        is_nan = set_optimizer_params_grad(param_optimizer, model.named_parameters(), test_nan=True)
                        if is_nan:
                            logger.info("FP16 TRAINING: Nan in gradients, reducing loss scaling")
                            args.loss_scale = args.loss_scale / 2
                            model.zero_grad()
                            continue
                        optimizer.step()
                        copy_optimizer_params_to_model(model.named_parameters(), param_optimizer)
                    else:
                        optimizer.step()
                    model.zero_grad()

            val_accuracy = compute_validation_accuracy(model, eval_dataloader, device)
            print("\nEpoch %d: Validation Accuracy=%.4f\n" % (i, val_accuracy))
            output_eval_file.write("Epoch %d: Validation Accuracy=%.4f\n" % (i, val_accuracy))
            output_eval_file.flush()

        model_path = os.path.join(args.output_dir, "model.pth")
        if n_gpu > 1:
            if not args.model_no_save:
                torch.save(model.module.state_dict(), model_path)
        else:
            if not args.model_no_save:
                torch.save(model.state_dict(), model_path) 

        output_eval_file.close()

    if args.do_eval and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
        if args.predict:
            outfile_path = os.path.join(args.output_dir, "predictions.txt")
            with open(outfile_path, "w") as fp:
                for input_ids, input_mask, segment_ids, article_ids in tqdm(eval_dataloader, desc="Evaluation"):
                    input_ids = input_ids.to(device)
                    input_mask = input_mask.to(device)
                    segment_ids = segment_ids.to(device)
                    article_ids = article_ids.to(device)

                    with torch.no_grad():
                        logits = model(input_ids, segment_ids, input_mask)

                    y_pred = logits.argmax(dim=1)

                    outfile = os.path.join(args.output_dir, "predictions.txt")
                    for article_id, pred in zip(article_ids, y_pred):
                        print(article_id.item(), end=" ", file=fp)
                        print(["false", "true"][pred.item()], file=fp)

        else:
            output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
            val_accuracy = compute_validation_accuracy(model, eval_dataloader, device)

            with open(output_eval_file, "w") as writer:
                logger.info("***** Eval results *****")
                logger.info("Validation Accuracy = %.4f", val_accuracy)
                writer.write("Validation Accuracy = %.4f\n" % (val_accuracy,))

if __name__ == "__main__":
    main()
