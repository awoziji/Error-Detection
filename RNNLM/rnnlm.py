# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================

"""Example / benchmark for building a PTB LSTM model.

Trains the model described in:
(Zaremba, et. al.) Recurrent Neural Network Regularization
http://arxiv.org/abs/1409.2329

There are 3 supported model configurations:
===========================================
| config | epochs | train | valid  | test
===========================================
| small  | 13     | 37.99 | 121.39 | 115.91
| medium | 39     | 48.45 |  86.16 |  82.07
| large  | 55     | 37.87 |  82.62 |  78.29
The exact results may vary depending on the random initialization.

The hyperparameters used in the model:
- init_scale - the initial scale of the weights
- learning_rate - the initial value of the learning rate
- max_grad_norm - the maximum permissible norm of the gradient
- num_layers - the number of LSTM layers
- num_steps - the number of unrolled steps of LSTM
- hidden_size - the number of LSTM units
- max_epoch - the number of epochs trained with the initial learning rate
- max_max_epoch - the total number of epochs for training
- keep_prob - the probability of keeping weights in the dropout layer
- lr_decay - the decay of the learning rate for each epoch after "max_epoch"
- batch_size - the batch size
- rnn_mode - the low level implementation of lstm cell: one of CUDNN,
             BASIC, or BLOCK, representing cudnn_lstm, basic_lstm, and
             lstm_block_cell classes.

The data required for this example is in the data/ dir of the
PTB dataset from Tomas Mikolov's webpage:

$ wget http://www.fit.vutbr.cz/~imikolov/rnnlm/simple-examples.tgz
$ tar xvf simple-examples.tgz

To run:

$ python ptb_word_lm.py --data_path=simple-examples/data/

"""
import time
import codecs
import numpy as np
from numpy import *
import tensorflow as tf
from my import reader
#import reader
from my import util
import os
from tensorflow.python.client import device_lib
import predict_result

flags = tf.flags
logging = tf.logging

flags.DEFINE_string("model", "train",
    "A type of model. Possible options are: train, test.")
flags.DEFINE_string("data_path", "./corpus/",
                    "Where the training/test data is stored.")
flags.DEFINE_string("save_path", "./model/model_total_e2/",
                    "Model output directory.")
flags.DEFINE_string("test_path", "./test/test_check",
                    "Model test file.")
flags.DEFINE_bool("use_fp16", False,
                  "Train using 16-bit floats instead of 32bit floats")
flags.DEFINE_integer("num_gpus", 1,
                     "If larger than 1, Grappler AutoParallel optimizer "
                     "will create multiple training replicas with each GPU "
                     "running one replica.")
flags.DEFINE_string("rnn_mode", "BLOCK",
                    "The low level implementation of lstm cell: one of CUDNN, "
                    "BASIC, and BLOCK, representing cudnn_lstm, basic_lstm, "
                    "and lstm_block_cell classes.")
FLAGS = flags.FLAGS
BASIC = "basic"
CUDNN = "cudnn"
BLOCK = "block"


def data_type():
  return tf.float16 if FLAGS.use_fp16 else tf.float32


class PTBInput(object):
  """The input data."""

  def __init__(self, config, data, seq_length, name=None):
    self.batch_size = batch_size = config.batch_size
    self.num_steps = num_steps = config.num_steps
    self.epoch_size = ((len(data) // batch_size) - 1) // num_steps
    self.input_data, self.targets, self.seq_length = reader.ptb_producer(
        data,seq_length, batch_size, num_steps, name=name)


class PTBModel(object):
  """The PTB model."""

  def __init__(self, is_training, config, input_):
    self._is_training = is_training
    self._input = input_
    self._rnn_params = None
    self._cell = None
    self.batch_size = input_.batch_size
    self.num_steps = input_.num_steps
    size = config.hidden_size
    self.vocab_size = len(reader.word_to_id)+1

    # Embedding part : Can use pre-trained embedding.
    with tf.device("/cpu:0"):
    #self.embedding = tf.get_variable(
    #    "embedding", [self.vocab_size, size], dtype=tf.float32)
    #self.embedding = tf.concat([tf.zeros([1,size]), self.embedding[1:]],axis=0 )
      if os.path.exists("./embedding.txt"):
        self.loadEmbedding()
      else:
        self.usePreEmbedding("../Keras/w2c_financial.txt")
      self.embedding = tf.get_variable(name = "embedding", initializer=tf.convert_to_tensor(self.embedding), dtype=tf.float32)
      #self.embedding = tf.Variable((self.embedding), dtype=tf.float32)
      inputs = tf.nn.embedding_lookup(self.embedding, input_.input_data)

    # get predict word's distribution
    output, state = self._build_rnn_graph(inputs, config, is_training)
    
    output = tf.contrib.layers.flatten(output)
    logits = tf.contrib.layers.fully_connected(output, self.vocab_size, activation_fn=None)

    # turn distribution into voca-size probability
    #softmax_w = tf.get_variable("softmax_w", [size, self.vocab_size], dtype=data_type())
    #softmax_b = tf.get_variable("softmax_b", [self.vocab_size], dtype=data_type())
    #logits = tf.nn.xw_plus_b(output, softmax_w, softmax_b)

    
    # Reshape logits to be a 3-D tensor for sequence loss
    logits = tf.reshape(logits, [self.batch_size, self.num_steps, self.vocab_size])
    # Use the contrib sequence loss and average over the batches
    loss = tf.contrib.seq2seq.sequence_loss(
        logits,
        input_.targets,
        tf.ones([self.batch_size, self.num_steps], dtype=data_type()),
        average_across_timesteps=False,
        average_across_batch=True)
    # Update the cost
    self._cost = tf.reduce_sum(loss)
    self._final_state = state

    self.logits = tf.nn.softmax(logits)
    if not is_training:
      return

    self._lr = tf.Variable(0.0, trainable=False)
    tvars = tf.trainable_variables()
    grads, _ = tf.clip_by_global_norm(tf.gradients(self._cost, tvars),
                                      config.max_grad_norm)
    optimizer = tf.train.GradientDescentOptimizer(self._lr)
    self._train_op = optimizer.apply_gradients(
        zip(grads, tvars),
        global_step=tf.train.get_or_create_global_step())

    self._new_lr = tf.placeholder(
        tf.float32, shape=[], name="new_learning_rate")
    self._lr_update = tf.assign(self._lr, self._new_lr)

  def resetInput(self, input_):
    self._input = input_


  def usePreEmbedding(self, embeddingf ,save = True):
    # use pre-trained embedding
    print("Using Pre-trained Embedding...")
    embedding_dict = {}
    f = codecs.open(embeddingf, "r", encoding="utf8",errors='ignore')
    for line in f:
      values = line.split()
      word = values[0]
      coefs = asarray(values[1:],dtype='float32')
      embedding_dict[word] = coefs
    f.close()
    self.embedding = zeros( (self.vocab_size, 300), dtype=float32)
    for word, i in reader.word_to_id.items():
      embedding_vector = embedding_dict.get(word)
      if embedding_vector is  None:
        embedding_vector = zeros((1,300), dtype=float32)
        for character in word:
          if character in embedding_dict:
            embedding_vector = embedding_vector + embedding_dict.get(character)
        embedding_vector = embedding_vector / len(word)
      self.embedding[i] = embedding_vector
    if save == True:
      self.saveEmebdding()
  
  def saveEmebdding(self):
    print("Saving Embedding...")
    ff = open("./embedding.txt","wb")
    for i in self.embedding:
      savetxt(ff,i,fmt="%f")
    ff.close()
    print("Saving Embedding Finish")

  def loadEmbedding(self):
    print("Loading Embedding...")
    self.embedding = []
    self.embedding  = loadtxt("./embedding.txt", dtype = float32)
    self.embedding = reshape(self.embedding,[-1,300])
    self.vocab_size = self.embedding.shape[0]
    print("Loading Embedding Finish")

  def _build_rnn_graph(self, inputs, config, is_training):
    if config.rnn_mode == CUDNN:
      return self._build_rnn_graph_cudnn(inputs, config, is_training)
    else:
      return self._build_rnn_graph_lstm(inputs, config, is_training)

  def _build_rnn_graph_cudnn(self, inputs, config, is_training):
    """Build the inference graph using CUDNN cell."""
    inputs = tf.transpose(inputs, [1, 0, 2])
    self._cell = tf.contrib.cudnn_rnn.CudnnLSTM(
        num_layers=config.num_layers,
        num_units=config.hidden_size,
        input_size=config.hidden_size,
        dropout=1 - config.keep_prob if is_training else 0)
    params_size_t = self._cell.params_size()
    self._rnn_params = tf.get_variable(
        "lstm_params",
        initializer=tf.random_uniform(
            [params_size_t], -config.init_scale, config.init_scale),
        validate_shape=False)
    c = tf.zeros([config.num_layers, self.batch_size, config.hidden_size],
                 tf.float32)
    h = tf.zeros([config.num_layers, self.batch_size, config.hidden_size],
                 tf.float32)
    self._initial_state = (tf.contrib.rnn.LSTMStateTuple(h=h, c=c),)
    outputs, h, c = self._cell(inputs, h, c, self._rnn_params, is_training)
    outputs = tf.transpose(outputs, [1, 0, 2])
    outputs = tf.reshape(outputs, [-1, config.hidden_size])
    return outputs, (tf.contrib.rnn.LSTMStateTuple(h=h, c=c),)

  def _get_lstm_cell(self, config, is_training):
    if config.rnn_mode == BASIC:
      return tf.contrib.rnn.BasicLSTMCell(
          config.hidden_size, forget_bias=0.0, state_is_tuple=True,
          reuse= not is_training)
    if config.rnn_mode == BLOCK:
      return tf.contrib.rnn.LSTMBlockCell(
          config.hidden_size, forget_bias=0.0)
    raise ValueError("rnn_mode %s not supported" % config.rnn_mode)

  def _build_rnn_graph_lstm(self, inputs, config, is_training):
    """Build the inference graph using canonical LSTM cells."""
    # Slightly better results can be obtained with forget gate biases
    def make_cell():
      cell = self._get_lstm_cell(config, is_training)
    # initialized to 1 but the hyperparameters of the model would need to be
    # different than reported in the paper.
      if is_training and config.keep_prob < 1:
        cell = tf.contrib.rnn.DropoutWrapper(
            cell, output_keep_prob=config.keep_prob)
      return cell

    cell = tf.contrib.rnn.MultiRNNCell(
        [make_cell() for _ in range(config.num_layers)], state_is_tuple=True)

    self._initial_state = cell.zero_state(config.batch_size, data_type())
    state = self._initial_state
    # Simplified version of tf.nn.static_rnn().
    # This builds an unrolled LSTM for tutorial purposes only.
    # In general, use tf.nn.static_rnn() or tf.nn.static_state_saving_rnn().
    #
    # The alternative version of the code below is:

    outputs, state = tf.nn.dynamic_rnn(cell, inputs, sequence_length = self._input.seq_length , initial_state=self._initial_state)
    
    """
    inputs = tf.unstack(inputs, num=self.num_steps, axis=1)
    outputs, state = tf.nn.static_rnn(cell, inputs, initial_state=self._initial_state)
    
    outputs = []
    with tf.variable_scope("RNN"):
      for time_step in range(self.num_steps):
        if time_step > 0: tf.get_variable_scope().reuse_variables()
        (cell_output, state) = cell(inputs[:, time_step, :], state)
        outputs.append(cell_output)
    """    

    #output = tf.transpose(outputs, [1, 0, 2])[-1]
    outputs = tf.transpose(outputs, [1, 0, 2])
    output = tf.reshape(outputs, [-1, config.hidden_size])
    #output = tf.reshape(tf.concat(outputs, 1), [-1, config.hidden_size])
    #print(output)
    return output, state

  def assign_lr(self, session, lr_value):
    session.run(self._lr_update, feed_dict={self._new_lr: lr_value})

  def export_ops(self, name):
    """Exports ops to collections."""
    self._name = name
    ops = {util.with_prefix(self._name, "cost"): self._cost}
    if self._is_training:
      ops.update(lr=self._lr, new_lr=self._new_lr, lr_update=self._lr_update)
      if self._rnn_params:
        ops.update(rnn_params=self._rnn_params)
    #else:
    ops.update({util.with_prefix(self._name, "output"):self.logits})
    for name, op in ops.items():
      tf.add_to_collection(name, op)
    self._initial_state_name = util.with_prefix(self._name, "initial")
    self._final_state_name = util.with_prefix(self._name, "final")
    util.export_state_tuples(self._initial_state, self._initial_state_name)
    util.export_state_tuples(self._final_state, self._final_state_name)

  def import_ops(self):
    """Imports ops from collections."""
    if self._is_training:
      self._train_op = tf.get_collection_ref("train_op")[0]
      self._lr = tf.get_collection_ref("lr")[0]
      self._new_lr = tf.get_collection_ref("new_lr")[0]
      self._lr_update = tf.get_collection_ref("lr_update")[0]

      rnn_params = tf.get_collection_ref("rnn_params")
      if self._cell and rnn_params:
        params_saveable = tf.contrib.cudnn_rnn.RNNParamsSaveable(
            self._cell,
            self._cell.params_to_canonical,
            self._cell.canonical_to_params,
            rnn_params,
            base_variable_scope="Model/RNN")
        tf.add_to_collection(tf.GraphKeys.SAVEABLE_OBJECTS, params_saveable)
    #else:
    self.logits = tf.get_collection_ref(util.with_prefix(self._name, "output"))[0]
    self._cost = tf.get_collection_ref(util.with_prefix(self._name, "cost"))[0]
    num_replicas = FLAGS.num_gpus if self._name == "Train" else 1
    self._initial_state = util.import_state_tuples(
        self._initial_state, self._initial_state_name, num_replicas)
    self._final_state = util.import_state_tuples(
        self._final_state, self._final_state_name, num_replicas)

  @property
  def input(self):
    return self._input

  @property
  def initial_state(self):
    return self._initial_state

  @property
  def output(self):
    return self.logits
  
  @property
  def cost(self):
    return self._cost

  @property
  def final_state(self):
    return self._final_state

  @property
  def lr(self):
    return self._lr

  @property
  def train_op(self):
    return self._train_op

  @property
  def initial_state_name(self):
    return self._initial_state_name

  @property
  def final_state_name(self):
    return self._final_state_name


class MediumConfig(object):
  """Medium config."""
  batch_size = 20
  max_grad_norm = 3
  learning_rate = 0.01

  keep_prob = 0.8
  lr_decay = 0.98
  init_scale = 0.05

  max_epoch = 8
  max_max_epoch = 1
  max_max_max_epoch = 2

  num_layers = 1
  hidden_size = 300
  num_steps = 47
  vocab_size = 9174
  rnn_mode = BLOCK

def run_epoch(session, model, eval_op=None, verbose=False, is_training=True, save_file=None):
  if is_training==False:
    result = session.run(model.logits)
    r1 = []
    for word in (result[0]):
      for backup in word:
        r1.append(backup)
        #save_file.write(str(backup)+" ")
      #save_file.write("\n")
    predict_result.genPredict(array(r1,dtype=float32), test_path = FLAGS.test_path)
    #np.savetxt(save_file,reshape(result,[-1,9175]),fmt="%.18e")
    return
  """Runs the model on the given data."""
  start_time = time.time()
  costs = 0.0
  iters = 0
  state = session.run(model.initial_state)

  fetches = {
      "cost": model.cost,
      "final_state": model.final_state,
  }
  if eval_op is not None:
    fetches["eval_op"] = eval_op
  for step in range(model.input.epoch_size):
    feed_dict = {}
    for i, (c, h) in enumerate(model.initial_state):
      feed_dict[c] = state[i].c
      feed_dict[h] = state[i].h

    vals = session.run(fetches, feed_dict)
    
    cost = vals["cost"]
    state = vals["final_state"]
    costs += cost
    iters += model.input.num_steps

    if verbose and step % (model.input.epoch_size // 10) == 10:
      print("%.3f perplexity: %.3f speed: %.0f wps" %
            (step * 1.0 / model.input.epoch_size, np.exp(costs / iters),
             iters * model.input.batch_size * max(1, FLAGS.num_gpus) /
             (time.time() - start_time)))

  return np.exp(costs / iters)


def get_config():
  """Get model config."""
  temconfig = MediumConfig()
  mode = 0
  if FLAGS.model == "test":
    mode = 1
  if FLAGS.rnn_mode:
    temconfig.rnn_mode = FLAGS.rnn_mode
  if FLAGS.num_gpus != 1 or tf.__version__ < "1.3.0" :
    temconfig.rnn_mode = BASIC
  return temconfig, mode


def main(_):
  if not FLAGS.data_path:
    raise ValueError("Must set --data_path to PTB data directory")
  gpus = [
      x.name for x in device_lib.list_local_devices() if x.device_type == "GPU"
  ]
  if FLAGS.num_gpus > len(gpus):
    raise ValueError(
        "Your machine has only %d gpus "
        "which is less than the requested --num_gpus=%d."
        % (len(gpus), FLAGS.num_gpus))

  config, mode = get_config()
  eval_config,mode = get_config()
  eval_config.keep_prob = 1
  eval_config.batch_size = 1

  if mode == 0:
    # train mode
    print("Enter Train Mode:")
    train_data,train_seq_length = reader.ptb_raw_data(FLAGS.data_path, is_training = True, index = 0)
    test_data,test_seq_length = reader.ptb_raw_data(FLAGS.test_path, is_training = False)
    with tf.Graph().as_default():
      initializer = tf.random_uniform_initializer(-config.init_scale,
                                                  config.init_scale)
      with tf.name_scope("Train"):
        train_input = PTBInput(config=config, data=train_data, seq_length= train_seq_length, name="TrainInput")
        with tf.variable_scope("Model", reuse=None , initializer=initializer) as scope:
          m = PTBModel(is_training=True, config=config, input_=train_input)
          scope.reuse_variables()
        tf.summary.scalar("Training Loss", m.cost)
        tf.summary.scalar("Learning Rate", m.lr)

      with tf.name_scope("Test"):
        test_input = PTBInput(config=eval_config, data=test_data, seq_length = test_seq_length, name="TestInput")
        with tf.variable_scope("Model", reuse=True , initializer=initializer) as scope:
          testm = PTBModel(is_training=False, config=eval_config, input_=test_input)
      #models = {"Train": m}
      models = {"Train": m,"Test":testm}
      for name, model in models.items():
        model.export_ops(name)
      metagraph = tf.train.export_meta_graph()
      if tf.__version__ < "1.1.0" and FLAGS.num_gpus > 1:
        raise ValueError("num_gpus > 1 is not supported for TensorFlow versions "
                         "below 1.1.0")
      soft_placement = False
      if FLAGS.num_gpus > 1:
        soft_placement = True
        util.auto_parallel(metagraph, m)

    with tf.Graph().as_default():
      tf.train.import_meta_graph(metagraph)
      m.is_training = True
      for model in models.values():
        model.import_ops()
      sv = tf.train.Supervisor(logdir=FLAGS.save_path)
      config_proto = tf.ConfigProto(allow_soft_placement=soft_placement)
      with sv.managed_session(config=config_proto) as session:
        for total_epoch in range(config.max_max_max_epoch):
          for train_round in range(21):
            print("=================")
            print("Now Training index: %d"%train_round)
            tf.reset_default_graph()
            train_data,train_seq_length = reader.ptb_raw_data(FLAGS.data_path, is_training = True, index = train_round)
            train_input = PTBInput(config=config, data=train_data, seq_length= train_seq_length, name="TrainInput")
            m.resetInput(train_input)
            for i in range(config.max_max_epoch):
              lr_decay = config.lr_decay ** max(i + 1 - config.max_epoch, 0.0)
              m.assign_lr(session, config.learning_rate * lr_decay)
              print("Epoch: %d Learning rate: %.3f" % (i + 1, session.run(m.lr)))
              train_perplexity = run_epoch(session, m, eval_op=m.train_op,
                                           verbose=True)
              print("Epoch: %d Train Perplexity: %.3f" % (i + 1, train_perplexity))
            
              length = reader.length
              #save_file = open("./result_proba_"+str(train_round)+".txt","w")
              print(length)
              for sublength in range(length):
                run_epoch(session,testm, is_training = False)
              #save_file.close()
              predict_result.saveResult(train_round, test_path = FLAGS.test_path)

              if os.path.exists(FLAGS.save_path):
                print("Saving model to %s." % FLAGS.save_path)
                sv.saver.save(session, FLAGS.save_path+"model.ckpt", global_step=sv.global_step)

  else:
    print("Enter Test Mode:")
    test_data,test_seq_length = reader.ptb_raw_data(FLAGS.test_path, is_training = False)
    length = reader.length
    config.keep_prob = 1
    config.batch_size = 1
    with tf.Graph().as_default():
      initializer = tf.random_uniform_initializer(-eval_config.init_scale,
                                                  eval_config.init_scale)
      with tf.name_scope("Train"):
        test_input = PTBInput(config=eval_config, data=test_data, seq_length = test_seq_length, name="TrainInput")
        with tf.variable_scope("Model", reuse=None, initializer=initializer):
          m = PTBModel(is_training=True, config=eval_config, input_=test_input)

      #tf.train.import_meta_graph("/media/zedom/Study/temp/-17148.meta")
      #m.is_training = False
      #m.import_ops()
      sv = tf.train.Supervisor(logdir=FLAGS.save_path)
      config_proto = tf.ConfigProto(allow_soft_placement=True)

      with sv.managed_session(config=config_proto) as session:
        ckpt = tf.train.get_checkpoint_state(checkpoint_dir=FLAGS.save_path)
        sv.saver.restore(session,ckpt.model_checkpoint_path)

        length = reader.length
        #save_file = open("./result_proba_"+str(train_round)+".txt","w")
        print(length)
        for sublength in range(length):
          run_epoch(session,m, is_training = False)
        #save_file.close()
        predict_result.saveResult(-1, test_path = FLAGS.test_path)

if __name__ == "__main__":
  tf.app.run()










"""
class LargeConfig(object):
  #Large config.
  init_scale = 0.04
  learning_rate = 1.0
  max_grad_norm = 10
  num_layers = 2
  num_steps = 35
  hidden_size = 1500
  max_epoch = 14
  max_max_epoch = 55
  keep_prob = 0.35
  lr_decay = 1 / 1.15
  batch_size = 20
  vocab_size = 10000
  rnn_mode = BLOCK


class TestConfig(object):
  #Tiny config, for testing.
  init_scale = 0.1
  learning_rate = 1.0
  max_grad_norm = 1
  num_layers = 1
  num_steps = 2
  hidden_size = 2
  max_epoch = 1
  max_max_epoch = 1
  keep_prob = 1.0
  lr_decay = 0.5
  batch_size = 20
  vocab_size = 10000
  rnn_mode = BLOCK

class SmallConfig(object):
  #Small config.
  init_scale = 0.1
  learning_rate = 1.0
  max_grad_norm = 5
  num_layers = 2
  num_steps = 20
  hidden_size = 200
  max_epoch = 4
  max_max_epoch = 13
  keep_prob = 1.0
  lr_decay = 0.5
  batch_size = 20
  vocab_size = 10000
  rnn_mode = BLOCK
"""