import os
import sys
import json
import pickle
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import time
from copy import deepcopy

common_path = os.path.abspath(os.path.join(__file__, "../../common"))
sys.path.append(common_path)

from cmd_args import cmd_args, logging
from scene2graph import Graph, GraphNode, Edge
from embedding import GNN, SceneDataset, GNNLocal, GNNGL, GNNGlobal
from torch_geometric.data import Data, DataLoader
from torch.autograd import Variable
from torch import autograd
from torch.distributions.categorical import Categorical
from tqdm import tqdm
from query import query
from utils import AVAILABLE_OBJ_DICT, policy_gradient_loss, get_reward, get_final_reward, Encoder
from env import Env
from decoder import AttClauseDecoder, NodeDecoder

class GDPolicy(nn.Module):

    def __init__(self, gnn, encoder, eps=cmd_args.eps, hidden_dim = cmd_args.hidden_dim):
        super().__init__()
        self.gnn = gnn
        self.encoder = encoder
        self.hidden_dim = hidden_dim
        self.reward_history = []
        self.prob_history = Variable(torch.Tensor())
        self.eps = eps
        self.decoder = None

    def get_prob_clause(self, graph_embedding, env):
        raise NotImplementedError

    def reset(self, eps=cmd_args.eps):
        self.eps = eps
        self.reward_history = []
        self.prob_history = Variable(torch.Tensor())

    def forward(self, env):
        prob, clause, next_state = self.get_prob_clause(env)

        if self.prob_history.dim() != 0:
            self.prob_history = torch.cat([self.prob_history, prob])
        else:
            self.prob_history = (prob)

        # update the environment
        env.clauses.append(clause)
        reward = env.step()
        env.state = next_state

        if env.is_finished() and cmd_args.reward_type == "only_success":
            if reward == -1:
                self.reward_history = [0] * len(self.reward_history)

        self.reward_history.append(reward)

        return env

class AttLSTMPolicy(GDPolicy):

    def __init__(self, gnn, encoder, eps=cmd_args.eps, hidden_dim = cmd_args.hidden_dim):
        super().__init__(gnn, encoder, eps, hidden_dim)
        self.clause_decoder = AttClauseDecoder(encoder, gnn.embedding_layer)
        # self.lstm_decoder = LSTMClauses(self.encoder, self.clause_decoder)

    def get_prob_clause(self, env):
        # initialize the state
        if type(env.state) == type(None):
            env.state = self.gnn(env.data)

        prob, clause, next_state = self.clause_decoder(env.state, self.encoder, self.eps)
        return prob, clause, next_state

# TODO: closer to the current design
class NodeSelPolicy(GDPolicy):
    def __init__(self, gnn, encoder, eps=cmd_args.eps, hidden_dim = cmd_args.hidden_dim):
        super().__init__(gnn, encoder, eps, hidden_dim)
        self.clause_decoder = NodeDecoder()

    def get_prob_clause(self, env):
        # initialize the state
        if type(env.state) == type(None):
            env.state = self.gnn(env.data)

        prob, clause, next_state = self.clause_decoder(env.state, env.graph, ref=None, eps=self.eps)
        return prob, clause, next_state

class RefRL():

    def __init__(self, dataset, config, graphs, hidden_dim=cmd_args.hidden_dim):

        embedding_layer = nn.Embedding(len(dataset.attr_encoder.lookup_list), cmd_args.hidden_dim)
        self.gnn = GNNGlobal(dataset, embedding_layer)
        self.encoder = dataset.attr_encoder

        self.graphs = graphs
        self.dataset = dataset
        train_num = int(len(dataset)*0.8)
        if train_num == len(dataset):
            train_num = -1
        # self.train_data = self.dataset[:train_num]
        # self.test_data = self.dataset[train_num:]
        self.train_data = self.dataset

        self.config = config
        self.hidden_dim = hidden_dim

        self.policy = AttLSTMPolicy(self.gnn, self.encoder)
        self.optimizer = torch.optim.Adam(self.policy.parameters(), lr=cmd_args.lr)
        self.episode_iter = cmd_args.episode_iter

        # for save and load
        self.iteration = 0

    def episode(self, data_point, graph, eps, attr_encoder):
        self.policy.reset(eps)
        retrain_list = []

        env = Env(data_point, graph, self.config, attr_encoder)
        iter_count = 0

        while not env.is_finished():
            # cannot figure out the clauses in limited step
            if (iter_count > cmd_args.episode_length):
                if cmd_args.reward_type == "only_success":
                    final_reward = get_final_reward(env)
                    if final_reward == -1:
                        self.policy.reward_history = [0.0] * len(self.policy.reward_history)
                        self.policy.reward_history.append(-1.0)
                    else:
                        self.policy.reward_history.append(1.0)
                    self.policy.reward_history = torch.tensor(self.policy.reward_history)
                else:
                    self.policy.reward_history.append(get_final_reward(env))
                    self.policy.prob_history = torch.cat([self.policy.prob_history, torch.tensor([1.0])])
                break

            iter_count += 1
            env = self.policy(env)

        logging.info(self.policy.reward_history)
        logging.info(self.policy.prob_history)
        logging.info(env.clauses)
        return env, retrain_list

def fit_one(refrl, data_point, graph, eps):
    global sub_ct
    sub_ct += 1

    if sub_ct > cmd_args.max_sub_prob:
        return None, None

    env, retrain_list = refrl.episode(data_point, graph, eps, refrl.dataset.attr_encoder)
    loss = policy_gradient_loss(refrl.policy.reward_history, refrl.policy.prob_history)

    if cmd_args.sub_loss:
        sub_loss = []
        for data_point, clauses in retrain_list:
            logging.info(f"clauses in env: {clauses}")
            e, r = fit_one(refrl, data_point, env.graph, eps)
            if e == None:
                print("Oops! running out of budget")
                return env, loss

            sub_loss.append(r)
        loss += sum(sub_loss)

    return env, loss

def fit(refrl):
    refrl.policy.train()
    print (refrl.policy.train())
    # refrl.train_data.shuffle()
    total_ct = 0
    data_loader = DataLoader(refrl.train_data)
    eps = cmd_args.eps
    print(type(eps))
    # with autograd.detect_anomaly():
    for it in range(cmd_args.episode_iter):

        logging.info(f"training iteration: {it}")
        success = 0

        total_loss = 0.0
        if refrl.iteration > it:
            continue

        for data_point, ct in zip(data_loader, tqdm(range(len(data_loader)))):
            global sub_ct
            sub_ct = 0
            total_ct += 1
            logging.info(ct)

            # if ct == 20000:
            #     print ("debug")
            #     continue
            graph = refrl.graphs[data_point.graph_id]
            env, loss = fit_one(refrl, data_point, graph, eps)

            total_loss += loss
            if env.success:
                success += 1

            if total_ct % cmd_args.batch_size == 0:

                refrl.optimizer.zero_grad()

            loss.backward()

            if total_ct % cmd_args.batch_size == 0:
                refrl.optimizer.step()

            if total_ct % cmd_args.save_num == 0 and not total_ct == 0:
                torch.save(refrl, cmd_args.model_path)

        logging.info(f"at train iter {it}, success num {success}, ave loss {total_loss/ct}")
        refrl.iteration += 1
        eps = cmd_args.eps_decay * eps

def test(refrl, split="test"):

    logging.info(f"testing on {split} data")
    refrl.policy.eval()

    if split == "train":
        data_loader = DataLoader(refrl.train_data)
    else:
        data_loader = DataLoader(refrl.test_data)

    success = 0
    avg_loss = 0
    eps = 0
    total_ct = 0

    for it in range(cmd_args.test_iter):
        logging.info(f"testing iteration: {it}")
        with torch.no_grad():
            for data_point, ct in zip(data_loader, tqdm(range(len(data_loader)))):

                logging.info(ct)
                total_ct += 1

                graph = refrl.graphs[data_point.graph_id]
                env, _ = refrl.episode(data_point, graph, eps, refrl.dataset.attr_encoder )
                loss = policy_gradient_loss(refrl.policy.reward_history, refrl.policy.prob_history)
                avg_loss += loss

                if env.success:
                    success += 1

    avg_loss /= total_ct
    logging.info(f"Testing {split}: success {success} out of {total_ct}, average loss is {avg_loss}")
