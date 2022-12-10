import argparse
import os
import shutil
import time
import random
import numpy as np
import math
import sys

import argparse
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import threading
from torch.distributions import Distribution
from typing import List

from .net import CNNPro
# from .kfac import KFACOptimizer

sys.path.append("../")
import config

class NNetWrapper():
    def __init__(self, net):
        super(NNetWrapper, self).__init__()
        self.net = net

        self.FixedCategorical = torch.distributions.Categorical
        old_sample = self.FixedCategorical.sample
        self.FixedCategorical.sample = lambda self: old_sample(self).unsqueeze(-1)

        log_prob_cat = self.FixedCategorical.log_prob
        self.FixedCategorical.log_probs = lambda self, actions: log_prob_cat(
            self, actions.squeeze(-1)).view(actions.size(0), -1).sum(-1).unsqueeze(-1)

        self.FixedCategorical.mode = lambda self: self.probs.argmax(dim=-1, keepdim=True)

        # self.optimizer = KFACOptimizer(self.net)
        self.optimizer = optim.RMSprop(self.net.parameters())

        self.logsoftmax = torch.nn.LogSoftmax(dim=-1)
        self.value_loss_fn = torch.nn.MSELoss()

    def predict(self, obs, mask, use_cuda=True):
        if use_cuda:
            device = torch.device("cuda:0")
        else:
            device = torch.device("cpu")

        self.net.eval()
        self.net.to(device)

        obs = np.expand_dims(obs, axis=0)
        mask = mask.reshape(1, len(mask))

        obs = torch.FloatTensor(obs).to(device)
        mask = torch.FloatTensor(mask).to(device)
        inverse_mask = (torch.ones_like(mask) - mask).to(device)

        action_logits, critic_values = self.net(obs)
        action_logits = action_logits - inverse_mask * 1e10

        action_probs = F.softmax(action_logits, dim=-1)
        action_probs = action_probs*mask
        action_dist = self.FixedCategorical(probs=action_probs)

        action_logits = action_logits[0]
        action_probs = action_probs[0]
        critic_values = critic_values[0]

        return action_logits, action_probs, action_dist, critic_values

    def train(self, trainExamples, use_cuda=True):
        acktr = False

        if use_cuda:
            device = torch.device("cuda:0")
        else:
            device = torch.device("cpu")

        self.net.to(device)

        for epoch in range(config.epochs):
            print('EPOCH ::: ' + str(epoch+1))
            self.net.train()

            batch_idx = 0

            while batch_idx < int(len(trainExamples)/config.batch_size):
                # sample_ids = np.random.randint(len(trainExamples), size=config.batch_size)
                sample_ids = list(range(len(trainExamples)))[batch_idx*config.batch_size:(batch_idx+1)*config.batch_size]

                # episode
                sample_obs, sample_masks, sample_actions, sample_action_policy, sample_critic_values = list(zip(*[trainExamples[i] for i in sample_ids]))
                sample_obs = torch.FloatTensor(np.array(sample_obs)).to(device)
                sample_masks = torch.FloatTensor(np.array(sample_masks)).to(device)
                inverse_masks = (torch.ones_like(sample_masks) - sample_masks).to(device)
                sample_actions = torch.FloatTensor(np.array(sample_actions)).to(device)
                sample_action_policy = torch.FloatTensor(np.array(sample_action_policy)).to(device)               
                sample_critic_values = torch.FloatTensor(np.array(sample_critic_values)).to(device)

                # network output
                action_logits, critic_values = self.net(sample_obs)
                action_logits = action_logits - inverse_masks * 1e10
                action_logsoftmax = self.logsoftmax(action_logits)

                action_probs = F.softmax(action_logits, dim=-1)
                action_probs = action_probs*sample_masks
                action_dist  = self.FixedCategorical(probs=action_probs)   

                action_log_probs = action_dist.log_probs(sample_actions)

                if acktr and (self.optimizer.steps % self.optimizer.Ts == 0):
                    # Sampled fisher, see Martens 2014
                    self.net.zero_grad()
                    pg_fisher_loss = -action_log_probs.mean()

                    value_noise = torch.randn(critic_values.size())
                    if critic_values.is_cuda:
                        value_noise = value_noise.cuda()

                    sample_values = critic_values + value_noise
                    vf_fisher_loss = -(critic_values - sample_values.detach()).pow(2).mean() # detach

                    fisher_loss = pg_fisher_loss + vf_fisher_loss
                    self.optimizer.acc_stats = True
                    fisher_loss.backward(retain_graph=True)
                    self.optimizer.acc_stats = False

                action_loss = torch.mean(torch.sum(-sample_action_policy*action_logsoftmax, dim=-1))
                value_loss = self.value_loss_fn(sample_critic_values, critic_values)
                
                total_loss = action_loss + value_loss

                # compute gradient and do SGD step
                self.optimizer.zero_grad()
                total_loss.backward()
                self.optimizer.step()

                batch_idx += 1

            self.save_checkpoint(folder="../" + config.epoch_dir, filename=config.save_model_name)
        

    def loss_pi(self, targets, outputs):
        return -torch.sum(targets*outputs)/targets.size()[0]

    def loss_v(self, targets, outputs):
        return torch.sum((targets-outputs.view(-1))**2)/targets.size()[0]

    def save_checkpoint(self, folder='checkpoint', filename='checkpoint.pth.tar'):
        filepath = os.path.join(folder, filename)
        if not os.path.exists(folder):
            print("Checkpoint Directory does not exist! Making directory {}".format(folder))
            os.mkdir(folder)
        torch.save({'state_dict' : self.net.state_dict(),}, filepath)

    def load_checkpoint(self, folder='checkpoint', filename='checkpoint.pth.tar'):
        filepath = os.path.join(folder, filename)
        if not os.path.exists(filepath):
            raise("No model in path {}".format(filepath))
        checkpoint = torch.load(filepath, map_location=torch.device("cpu"))
        self.net.load_state_dict(checkpoint['state_dict'])

