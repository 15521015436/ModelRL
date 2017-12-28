from MDP_learning.multi_agent import make_env2
import MDP_learning.multi_agent.policies as MAPolicies

from MDP_learning.helpers import build_models
from collections import deque
import random
import numpy as np
# matplotlib.use('GTK3Cairo', warn=False, force=True)
import gym

from MDP_learning.helpers.logging_model_learner import LoggingModelLearner


class ModelLearner(LoggingModelLearner):
    def __init__(self, environment, agent_id, action_size,
                 mem_size=3000, epochs=4, learning_rate=.001,
                 sequence_length=0, write_tboard=True, env_name=None):
        super().__init__(environment, sequence_length,
                         write_tboard=write_tboard,
                         out_dir_add=
                         'agentID{}{}'.format(
                             agent_id,
                             '_scenario_{}'.format(env_name) if env_name is not None else ''))

        self.learning_rate = learning_rate
        self.net_train_epochs = epochs
        self.mem_size = mem_size

        self.x_memory = deque(maxlen=mem_size)
        self.y_memory = deque(maxlen=mem_size)

        self.agent_id = agent_id
        self.policy = MAPolicies.RandomPolicy(env, self.agent_id)

        self.tmodel = build_models.build_regression_model(
            self.env.observation_space[self.agent_id].shape[0] + action_size,
            1 + self.env.observation_space[self.agent_id].shape[0])

        self.models = [self.tmodel]
        self.save_model_config()

    def get_action(self, obs_n):
        return self.policy.action(obs_n[self.agent_id])

    def append_to_mem(self, obs, act, reward, obs_next, done):
        # TODO is there any point in learning done in this environment
        self.x_memory.append(np.concatenate((obs, act)))
        self.y_memory.append(np.concatenate(([reward], obs_next)))

    def clear_mem(self):
        self.x_memory.clear()
        self.y_memory.clear()

    def train_models(self, minibatch_size=32):
        self.tmodel.fit(np.array(self.x_memory),
                        np.array(self.y_memory),
                        batch_size=minibatch_size,
                        epochs=self.net_train_epochs,
                        validation_split=0.1,
                        callbacks=self.Ttensorboard,
                        verbose=1)
        self.save()


class MultiAgentModelLearner(LoggingModelLearner):
    def __init__(self, environment, mem_size=3000, epochs=4, learning_rate=.001,
                 sequence_length=0, write_tboard=True, scenario_name=None):
        super().__init__(environment, sequence_length,
                         write_tboard=write_tboard,
                         out_dir_add='scenario_name{}'.format(scenario_name) if scenario_name is not None else None)
        self.render = True

        self.mem_size = mem_size
        self.x_memory = deque(maxlen=self.mem_size)
        self.y_memory = deque(maxlen=self.mem_size)

        # if all actions are visible we need to sum them
        action_compound_size = 0
        for ii in range(self.env.n):
            size_act, _ = MAPolicies.get_action_and_comm_actual_size(self.env, ii)
            action_compound_size += size_act

        self.local_learners = []
        for i in range(self.env.n):
            self.local_learners.append(
                ModelLearner(environment, i, action_compound_size,
                             mem_size=mem_size,
                             epochs=epochs,
                             learning_rate=learning_rate,
                             sequence_length=sequence_length,
                             write_tboard=write_tboard,
                             env_name=scenario_name))

    # get action from model using random policy
    def get_action(self, obs_n):
        act_n = []
        for ii, ll in enumerate(self.local_learners):
            act_n.append(ll.get_action(obs_n[ii]))
        return act_n

    def get_transition(self, act_n):
        # step environment
        obs_n_next, reward_n, done_n, info_n = self.env.step(act_n)
        # get the real action
        act_n_real = [agent.action.u for agent in self.env.agents]
        return obs_n_next, act_n_real, reward_n, done_n, info_n

    def fill_memory(self):
        # execution loop
        self.x_memory.clear()
        self.y_memory.clear()
        for ll in self.local_learners:
            ll.clear_mem()

        obs_n = env.reset()
        for ii in range(self.mem_size):
            # query for action from each agent's policy
            act_n = self.get_action(obs_n)

            # do a transition
            obs_n_next, act_n_real, reward_n, done_n, info_n = self.get_transition(act_n)

            # save the sample <s, a, r, s'>
            # TODO is there any point in learning done in this environment
            self.x_memory.append((np.array(obs_n).flatten(), np.array(act_n_real).flatten()))
            self.y_memory.append((np.array(reward_n).flatten(), np.array(obs_n_next).flatten()))
            for jj, ll in enumerate(self.local_learners):
                ll.append_to_mem(obs_n[jj], np.array(act_n_real).flatten(),  # LET ALL AGENTS SEE ALL ACTIONS
                                 reward_n[jj], obs_n_next[jj], done_n[jj])

            obs_n = obs_n_next

            # render and rest?
            if self.render:
                env.render()
            if any(done_n):
                print("DONE")
                break

    def setup_batch_for_RNN(self, batch):
        raise NotImplementedError

    # pick samples randomly from replay memory (with batch_size)
    def train_model(self, minibatch_size=32):
        for ii, ll in enumerate(self.local_learners):
            ll.train_models(minibatch_size=minibatch_size)

    def step_model(self, state, action):
        raise NotImplementedError

    def run(self, environment, rounds=1):
        for e in range(rounds):
            data = self.fill_memory()
            self.train_model(data)

    def evaluate(self, environment, do_plots=False):
        raise NotImplementedError


if __name__ == "__main__":
    env_name = 'simple_spread'
    env = make_env2.make_env(env_name)

    canary = MultiAgentModelLearner(env, mem_size=128, sequence_length=0, scenario_name=env_name)
    canary.run(env, rounds=100)

    # print('MSE: {}'.format(canary.evaluate(env)))
