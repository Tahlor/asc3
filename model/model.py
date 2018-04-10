import tensorflow as tf
import tensorflow.contrib.layers as tfcl
from tensorflow.contrib.rnn import GRUCell
import tensorflow.contrib.legacy_seq2seq as seq2seq
import math
import sys
sys.path.append("..")
# import model.value
# import model.policy


def fc(inputs, num_nodes, name='0', activation=tf.nn.relu):
    with tf.variable_scope('fully_connected', reuse=tf.AUTO_REUSE) as scope:
        weights = tf.get_variable('W_' + name,
                                  shape=(inputs.shape[1], num_nodes),
                                  dtype=tf.float32,
                                  initializer=tfcl.variance_scaling_initializer())

        bias = tf.get_variable('b_' + name,
                               shape=[num_nodes],
                               dtype=tf.float32,
                               initializer=tfcl.variance_scaling_initializer())

        net_value = tf.matmul(inputs, weights) + bias
        if activation is None:
            return net_value
        else:
            return activation(net_value)

def fc_list(inputs, num_nodes, name='0', activation=tf.nn.relu):
    outputs = []
    for item in inputs:
        outputs.append(fc(item, num_nodes, name=name, activation=activation))

    return tf.concat(outputs, axis=1)


def get_gru(num_layers, state_dim, reuse=False):
    with tf.variable_scope('gru', reuse=reuse):
        gru_cells = []
        for _ in range(num_layers):
            gru_cells.append(GRUCell(state_dim))

    return gru_cells


class Model:
    def __init__(self, batch_size=1, inputs_per_time_step=2, seq_length=1000, num_layers=1, layer_size=256, trainable = True, discount = .9, naive=False):
        self.seq_length = seq_length
        self.batch_size = batch_size
        self.input_size = inputs_per_time_step * seq_length
        self.num_layers = num_layers
        self.layer_size = layer_size
        self.number_of_actions = 1
        self.inputs_ph = None
        self.targets_ph = None # useless for 'real' model, just here for the proof of concept
        self.actions_op = None
        self.value_op = None
        self.loss_op = None
        self.optimizer = None
        self.saver = None
        self.trainable = trainable
        self.graph = tf.Graph()
        self.discount = discount
        self.entropy_weight = 1e-4
        self.naive = naive
        self.build_network()


    def get_params(self):
        return {"input_size":self.input_size, "layer_size":self.layer_size, "trainable": self.trainable, "discount":self.discount}

    def build_network(self):
        with self.graph.as_default():
            self.inputs_ph = tf.placeholder(tf.float32, shape=[self.batch_size, self.input_size], name='inputs')
            self.targets_ph = tf.placeholder(tf.float32, shape=[self.batch_size, self.input_size], name='targets')
            self.gru_state_ph = tf.placeholder(tf.float32, shape=[self.batch_size, self.layer_size], name='gru_state')
            self.policy_advantage = tf.placeholder(tf.float32, shape=[self.batch_size, self.seq_length], name='advantages')
            self.chosen_actions = tf.placeholder(tf.float32, shape=[self.batch_size, self.seq_length, self.number_of_actions], name='chosen_actions')
            self.discounted_rewards = tf.placeholder(tf.float32, shape=[self.batch_size, self.seq_length], name='discounted_rewards')

            inputs = tf.split(self.inputs_ph, self.seq_length, axis=1)

            if self.naive:
                output_list = fc_list(self.inputs_ph, self.layer_size)

            else:
                gru_cells = get_gru(self.num_layers, self.layer_size)
                self.multi_cell = tf.nn.rnn_cell.MultiRNNCell(gru_cells)
                # initial_state = self.multi_cell.zero_state(batch_size=self.batch_size, dtype=tf.float32)
                initial_state = tuple([self.gru_state_ph for _ in range(self.num_layers)])

                with tf.variable_scope('rnn_decoder') as scope:
                    # network_output is a tuple of (output_list, final_state)
                    # note that (output_list) is really just the GRU state at each time step
                    # (e.g. the final element in output_list is equal to final_state)
                    self.network_output = seq2seq.rnn_decoder(inputs, initial_state, self.multi_cell)
                    output_list = self.network_output[0]
                    final_state = self.network_output[1]

            # Approach for a discrete action space, where we can either
            # buy or sell but don't specify an amount
            # logits = fc(output, 2, name='logits')
            # actions = tf.nn.softmax(logits)

            # Approach for a continuous space.
            # 'Action' is a real number in [-1,1], where
            # -1 means 'sell everything you have',
            # 0 means 'do nothing', and
            # 1 means 'buy everything you can'.
            # Exchange should know how to interpret this number.

            # Actions distribution: [batch_size x seq_length x number_of_actions x 2]
            # i.e. one mu and one standard deviation for each action at each step of each sequence
            actions_raw = fc_list(output_list, self.number_of_actions * 2, name='action', activation=None)
            self.actions_op = tf.reshape(actions_raw, [self.batch_size, self.seq_length, self.number_of_actions, 2])
            self.action_mu = tf.nn.tanh(self.actions_op[:, :, :, 0])
            self.action_sd = tf.nn.softplus(self.actions_op[:, :, :, 1])

            # Value: [batch_size x seq_length]
            # i.e. one value per step in the sequence, for all sequences
            self.value_op = fc_list(output_list, 1, name='value', activation=None)

            # self.loss_op = tf.reduce_sum(self.targets_ph - self.actions_op, axis=1)
            # self.optimizer = tf.train.RMSPropOptimizer(0.01).minimize(self.loss_op)

            #with tf.Session(graph=self.graph) as sess:
            #    sess.run(tf.global_variables_initializer())

            self.saver = tf.train.Saver()


    def update_policy(self):
        # input placeholder = input
        # GRU states placeholder = states

        # Actions [batch size, t steps, # of actions, 2 (action, sd)]
        # chosen actions = [ batch size=1 * t ]
        # chosen rewards = [ batch size=1 * t ]
        # value_op = [batch_size, t]
        # actions_mus    = [batch, t, # of actions]

        # Vector of continuous probabilites for each action
        # Vector of covariances for each action

        action_dist = tf.contrib.distributions.Normal(self.action_mus, self.action_sds) # [batch, t, # of actions]

        # Get log prob given chosen actions
        log_prob = action_dist.log_prob(self.chosen_actions) # probability < 1 , so negative value here

        # Calculate entropy
        # entropy = -1/2 * (tf.log(2*self.action_mus * math.pi * self.action_sds ** 2) + 1) # N steps X # of actions
        entropy = log_prob.entropy() # [batch, t, # of actions], negative

        # Advantage function - exogenous to the policy network
        # advantage = tf.subtract(self.rewards, self.value_op, name='advantage')  #[ batch size=1 * t ]

        # Loss -- entropy is higher with high uncertainty -- ensures exploration at first,
        #  e.g. even if an OK path is found at first, high entropy => higher loss, so it will take
        #   that good path with a grain of salt
        self.policy_loss =  -tf.reduce_mean(log_prob * self.policy_advantages + entropy * self.entropy_weight)

        self.optimizer = tf.train.RMSPropOptimizer(0.00025, 0.99, 0.0, 1e-6)
        self.policy_grads_and_vars = self.optimizer.compute_gradients(self.policy_loss)
        self.policy_grads_and_vars = [[grad, var] for grad, var in self.policy_grads_and_vars if grad is not None]
        self.policy_train_op = self.optimizer.apply_gradients(self.policy_grads_and_vars, global_step=tf.contrib.framework.get_global_step())
        return self.policy_train_op

    def update_value(self):

        #self.value_losses = (self.value_op - self.discounted_rewards)**2
        self.value_losses = tf.squared_difference(self.value_op, self.discounted_rewards)
        self.value_loss = tf.reduce_sum(self.value_losses, name="value_loss")

        #self.optimizer = tf.train.AdamOptimizer(1e-4)
        self.optimizer = tf.train.RMSPropOptimizer(0.00025, 0.99, 0.0, 1e-6)
        self.value_grads_and_vars = self.optimizer.compute_gradients(self.value_loss)
        self.value_grads_and_vars = [[grad, var] for grad, var in self.value_grads_and_vars if grad is not None]
        self.value_train_op = self.optimizer.apply_gradients(self.value_grads_and_vars, global_step=tf.contrib.framework.get_global_step())
        return self.value_train_op


    # tf.contrib.distributions.Normal(1.,1.).log_prob()

    def get_actions_states_values(self, sess, input_tensor, gru_state):
        actions, states, values = sess.run([self.actions_op, self.network_output, self.value_op], feed_dict={self.inputs_ph: input_tensor, self.gru_state_ph: gru_state})
        return actions, tuple(states[0]), values

    def get_state(self):
        return self.last_input_state, self.gru_state_ph

    def get_value(self, sess, input, gru_state = None):
        with tf.Session() as sess:
            value = sess.run(self.value_op, feed_dict={self.input_ph: input, self.gru_state_input: gru_state})
        return value, gru_state

    def get_policy(self, sess, input, gru_state = None):
        with tf.Session() as sess:
            policy = sess.run(self.policy_op, feed_dict={self.input_ph: input, self.gru_state_input: gru_state})
        return policy, gru_state

