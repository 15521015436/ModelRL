from __future__ import division
import argparse
import random

from PIL import Image
import numpy as np
import gym

from keras.models import Sequential, Model
from keras.layers import Dense, Activation, Flatten, Convolution2D, Permute, Input, Embedding, LSTM, concatenate, \
    Reshape
from keras.optimizers import Adam
import keras.backend as K

from rl.agents.dqn import DQNAgent
from rl.policy import LinearAnnealedPolicy, BoltzmannQPolicy, EpsGreedyQPolicy
from rl.memory import SequentialMemory
from rl.core import Processor
from rl.callbacks import FileLogger, ModelIntervalCheckpoint

from keras.callbacks import TensorBoard

INPUT_SHAPE = (84, 84)
WINDOW_LENGTH = 4
nb_steps_dqn_fit = 1750000  # 1750000
nb_steps_warmup_dqn_agent = int(max(0, np.sqrt(nb_steps_dqn_fit))) * 42 + 1000  # 50000
target_model_update_dqn_agent = int(max(0, np.sqrt(nb_steps_dqn_fit))) * 8 + 8  # 10000
memory_limit = nb_steps_dqn_fit  # 1000000
nb_steps_annealed_policy = int(nb_steps_dqn_fit / 2)  # 1000000


class AtariProcessor(Processor):
    def process_observation(self, observation):
        assert observation.ndim == 3  # (height, width, channel)
        img = Image.fromarray(observation)
        img = img.resize(INPUT_SHAPE).convert('L')  # resize and convert to grayscale
        processed_observation = np.array(img)
        assert processed_observation.shape == INPUT_SHAPE
        return processed_observation.astype('uint8')  # saves storage in experience memory

    def process_state_batch(self, batch):
        # We could perform this processing step in `process_observation`. In this case, however,
        # we would need to store a `float32` array instead, which is 4x more memory intensive than
        # an `uint8` array. This matters if we store 1M observations.
        processed_batch = batch.astype('float32') / 255.
        return processed_batch

    def process_reward(self, reward):
        return np.clip(reward, -1., 1.)


parser = argparse.ArgumentParser()
parser.add_argument('--mode', choices=['train', 'test'], default='train')
parser.add_argument('--env-name', type=str, default='BreakoutDeterministic-v4')
parser.add_argument('--weights', type=str, default=None)
args = parser.parse_args()

# Get the environment and extract the number of actions.
env = gym.make(args.env_name)
np.random.seed(123)
env.seed(123)
nb_actions = env.action_space.n

class ModelBasedLearner:

    def __init__(self, env, input_shape, action_shape=(1, ), sequence_length=10):
        self.env = env
        self.sequence_length = sequence_length
        self.input_shape = input_shape
        self.action_shape = action_shape
        self.nb_actions = self.env.action_space.n

    def processor_net(self):
        self.input_shape = (WINDOW_LENGTH,) + INPUT_SHAPE
        image_in = Input(shape=self.input_shape, name='main_input')
        input_perm = Permute((2, 3, 1), input_shape=self.input_shape)(image_in)
        conv1 = Convolution2D(32, 8, 8, subsample=(4, 4), activation='relu')(input_perm)
        conv2 = Convolution2D(64, 4, 4, subsample=(2, 2), activation='relu')(conv1)
        conv3 = Convolution2D(64, 3, 3, subsample=(1, 1), activation='relu')(conv2)
        conv_out = Flatten(name='flat_feat')(conv3)
        processor_model = Model(inputs=[image_in], outputs=[conv_out])
        processor_model.compile('rmsprop', loss='mse', metrics=['accuracy'])
        self.out_proc_units = int(np.prod(conv3.shape[1:]))
        print(processor_model.summary())
        return processor_model

    def ml_net_ff(self):
        state_in = Input(shape=self.out_proc_units)
        action_in = Input(shape=self.action_shape, name='action_input')
        state_and_action = concatenate([state_in, action_in], name='state_and_action')
        dense = Dense(512, activation='relu')(state_and_action)
        state_pred = Dense(int(np.prod(conv3.shape[1:])), activation='relu', name='predicted_next_state')(dense)
        reward_pred = Dense(1, activation='linear', name='predicted_reward')(dense)
        terminal_pred = Dense(1, activation='sigmoid', name='predicted_terminal')(dense)
        ml_model = Model(inputs=[state_in, action_in], outputs=[state_pred, reward_pred, terminal_pred])
        ml_model.compile('rmsprop', loss={'predicted_next_state': 'mae', 'predicted_reward': 'mse',
                                          'predicted_terminal': 'binary_crossentropy'}, metrics=['accuracy'])
        print(ml_model.summary())
        return ml_model

    def q_net(self):
        state_in = Input(shape=self.out_proc_units)
        action_in = Input(shape=self.action_shape, name='action_input')
        state_and_action = concatenate([state_in, action_in], name='state_and_action')
        dense = Dense(512, activation='relu')(state_and_action)
        q_out = Dense(nb_actions, activation='linear')(dense)
        q_model = Model(inputs=[state_in, action_in], outputs=[q_out])
        print(q_model.summary())
        return q_model





# Define memory and image pre-processing
memory = SequentialMemory(limit=memory_limit,
                          #window_length=WINDOW_LENGTH
                          )

processor = AtariProcessor()

# Select a policy
policy = LinearAnnealedPolicy(EpsGreedyQPolicy(), attr='eps', value_max=1., value_min=.1, value_test=.05,
                              nb_steps=nb_steps_annealed_policy)

dqn = DQNAgent(model=model, nb_actions=nb_actions, policy=policy, memory=memory,
               processor=processor, nb_steps_warmup=nb_steps_warmup_dqn_agent, gamma=.99,
               target_model_update=target_model_update_dqn_agent,
               train_interval=4, delta_clip=1.)
dqn.compile(Adam(lr=.00025), metrics=['mae'])

if args.mode == 'train':
    # Okay, now it's time to learn something! We capture the interrupt exception so that training
    # can be prematurely aborted. Notice that you can the built-in Keras callbacks!
    weights_filename = 'dqn_{}_weights.h5f'.format(args.env_name)
    checkpoint_weights_filename = 'dqn_' + args.env_name + '_weights_{step}.h5f'
    log_filename = 'dqn_{}_log.json'.format(args.env_name)
    callbacks = [ModelIntervalCheckpoint(checkpoint_weights_filename, interval=250000)]
    callbacks += [FileLogger(log_filename, interval=100)]
    dqn.fit(env, callbacks=callbacks, nb_steps=nb_steps_dqn_fit, log_interval=10000)

    # After training is done, we save the final weights one more time.
    dqn.save_weights(weights_filename, overwrite=True)

    # Finally, evaluate our algorithm for 10 episodes.
    # dqn.test(env, nb_episodes=1, visualize=False)
    ########################################################################################################################
    model_truncated = Model(inputs=dqn.model.input, outputs=dqn.model.get_layer('flat_feat').output)
    print(model_truncated.summary())

    data_size = dqn.memory.observations.length
    batch_size = 50000
    n_epochs = 9 * int(data_size / batch_size) + 1  # go through data n times
    for ii in range(n_epochs):
        hstates = np.empty((batch_size, sequence_length, int(np.prod(conv3.shape[1:]))), dtype=np.float32)
        actions = np.empty((batch_size, sequence_length, 1), dtype=np.float32)
        next_hstate = np.empty((batch_size, int(np.prod(conv3.shape[1:]))), dtype=np.float32)
        rewards = np.empty((batch_size, 1), dtype=np.float32)
        terminals = np.empty((batch_size, 1), dtype=np.float32)

        for jj in range(batch_size):
            # check for terminals
            start = random.randrange(data_size - (sequence_length + 1))
            experiences = dqn.memory.sample(sequence_length + 1, range(start, start + sequence_length + 1))
            while np.array([e.terminal1 for e in experiences]).any():
                start = random.randrange(data_size - (sequence_length + 1))
                experiences = dqn.memory.sample(sequence_length + 1, range(start, start + sequence_length + 1))

            # Start by extracting the necessary parameters (we use a vectorized implementation).
            state0_seq = []
            # state1_seq = []
            reward_seq = []
            action_seq = []
            terminal1_seq = []

            for e in experiences:
                state0_seq.append(e.state0)
                # state1_seq.append(e.state1)
                reward_seq.append(e.reward)
                action_seq.append(e.action)
                terminal1_seq.append(e.terminal1)

            state0_seq = dqn.process_state_batch(state0_seq)
            # state1_seq = dqn.process_state_batch(state1_seq)
            reward_seq = np.array(reward_seq)
            action_seq = np.array(action_seq, dtype=np.float32)
            terminal1_seq = np.array(terminal1_seq)

            hidden_states_seq = model_truncated.predict_on_batch(state0_seq)

            hstates[jj, ...] = hidden_states_seq[np.newaxis, :-1, :]
            actions[jj, ...] = action_seq[np.newaxis, :-1, np.newaxis]
            next_hstate[jj, ...] = hidden_states_seq[np.newaxis, -1, :]
            rewards[jj, ...] = reward_seq[np.newaxis, -1]
            terminals[jj, ...] = terminal1_seq[np.newaxis, -1]

        ml_model.fit([hstates, actions], [next_hstate, rewards, terminals], verbose=1, epochs=2,
                     callbacks=[TensorBoard(log_dir='./logs/Tlearn')])

    # #######################################################################################################################
    from collections import deque


    class SynthEnv():
        def __init__(self, tmodel, conv_model, real_env, processor, sequence_len):
            self.tmodel = tmodel
            self.conv_model = conv_model
            self.real_env = real_env
            self.processor = processor
            self.seq_len = sequence_len
            self.action_space = real_env.action_space
            self.observation_space = gym.spaces.Box(-10, 10, (int(np.prod(conv3.shape[1:])),))
            self.state_seq, self.action_seq = self.init_state()

        def init_state(self):
            state_seq = deque(maxlen=self.seq_len)
            act_seq = deque(maxlen=self.seq_len)  # TODO should be just one action
            self.real_env.reset()

            images = []
            for _ in range(self.seq_len + WINDOW_LENGTH):
                act_seq.append(self.real_env.action_space.sample())
                obs, rw, dn, info = self.real_env.step(act_seq[-1])
                obs = processor.process_observation(obs)
                images.append(obs)

            for i in range(self.seq_len):
                state_seq.append(
                    self.conv_model.predict(
                        np.expand_dims(np.array(images[i:i + WINDOW_LENGTH]), axis=0)
                    )
                )

            return state_seq, act_seq

        def step(self, action):
            # TODO append action before?
            self.action_seq.append(action)
            # reshape
            ssq = np.rollaxis(np.array(self.state_seq), 1)
            asq = np.expand_dims(np.expand_dims(np.array(self.action_seq), axis=0), axis=2)
            next_state, reward, done = self.tmodel.predict([ssq, asq])
            self.state_seq.append(next_state)
            # unwrap and add empty info
            return next_state[0], float(reward[0, 0]), bool(done[0, 0] > .5), {}
        # TODO done might never occur in unseen territory
        # TODO check timing t->t+1

        def reset(self):
            self.state_seq, self.action_seq = self.init_state()
            return self.state_seq[-1].flatten()


    env2 = SynthEnv(ml_model, model_truncated, env, processor, sequence_length)

    hidden_in = Input(shape=(1, int(np.prod(conv3.shape[1:]))), name='hidden_input')
    hidden_in_f = Flatten(name='flat_hidden')(hidden_in)
    dense_out = Dense(512, activation='relu')(hidden_in_f)
    q_out = Dense(nb_actions, activation='linear')(dense_out)
    model2 = Model(inputs=[hidden_in], outputs=[q_out])
    print(model2.summary())

    memory2 = SequentialMemory(limit=memory_limit, window_length=1)
    policy2 = LinearAnnealedPolicy(EpsGreedyQPolicy(), attr='eps', value_max=1., value_min=.1, value_test=.05,
                                   nb_steps=nb_steps_annealed_policy)
    dqn2 = DQNAgent(model=model2, nb_actions=nb_actions, policy=policy2, memory=memory2,
                    nb_steps_warmup=nb_steps_warmup_dqn_agent, gamma=.99,
                    target_model_update=target_model_update_dqn_agent,
                    train_interval=4, delta_clip=1.)
    dqn2.compile(Adam(lr=.00025), metrics=['mae'])
    dqn2.fit(env2, callbacks=callbacks, nb_steps=nb_steps_dqn_fit, log_interval=10000)

    # #######################################################################################################################

    image_in = Input(shape=input_shape, name='main_input')
    input_perm = Permute((2, 3, 1), input_shape=input_shape)(image_in)
    conv1 = Convolution2D(32, 8, 8, subsample=(4, 4), activation='relu')(input_perm)
    conv2 = Convolution2D(64, 4, 4, subsample=(2, 2), activation='relu')(conv1)
    conv3 = Convolution2D(64, 3, 3, subsample=(1, 1), activation='relu')(conv2)
    conv_out = Flatten(name='flat_feat')(conv3)
    dense_out = Dense(512, activation='relu')(conv_out)
    q_out = Dense(nb_actions, activation='linear')(dense_out)
    model3 = Model(inputs=[image_in], outputs=[q_out])

    # Combine truncated and model2 top
    wghts = [np.zeros(w.shape) for w in model3.get_weights()]
    for layer, w in enumerate(model_truncated.get_weights()):
        wghts[layer] = w
    depth_conv = len(model_truncated.get_weights())
    for layer, w in enumerate(dqn2.model.get_weights()):
        wghts[layer + depth_conv] = w
    model3.set_weights(wghts)
    print(model3.summary())

    memory3 = SequentialMemory(limit=memory_limit, window_length=1)
    policy3 = LinearAnnealedPolicy(EpsGreedyQPolicy(), attr='eps', value_max=1., value_min=.1, value_test=.05,
                                   nb_steps=nb_steps_annealed_policy)
    dqn3 = DQNAgent(model=model3, nb_actions=nb_actions, policy=policy3, memory=memory3,
                    processor=processor, nb_steps_warmup=nb_steps_warmup_dqn_agent, gamma=.99,
                    target_model_update=target_model_update_dqn_agent,
                    train_interval=4, delta_clip=1.)
    dqn3.compile(Adam(lr=.00025), metrics=['mae'])
    dqn.test(env, nb_episodes=10, visualize=True)
    dqn3.test(env, nb_episodes=10, visualize=True)
    ########################################################################################################################

elif args.mode == 'test':
    weights_filename = 'dqn_{}_weights.h5f'.format(args.env_name)
    if args.weights:
        weights_filename = args.weights
    dqn.load_weights(weights_filename)
    dqn.test(env, nb_episodes=10, visualize=True)
