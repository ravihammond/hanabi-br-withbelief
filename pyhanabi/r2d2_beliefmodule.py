# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#
import torch
import torch.multiprocessing as mp
import torch.nn as nn
import torch.nn.functional as F
import time
from typing import Tuple, Dict
import common_utils
from transformer_embedding import get_model
from td_methods import compute_belief

class R2D2Net(torch.jit.ScriptModule):
    __constants__ = [
        "hid_dim",
        "out_dim",
        "num_lstm_layer",
        "hand_size",
        "skip_connect",
    ]

    def __init__(
        self,
        device,
        in_dim,
        hid_dim,
        out_dim,
        num_lstm_layer,
        hand_size,
        num_fc_layer,
        skip_connect,
    ):
        super().__init__()
        self.in_dim = in_dim
        self.hid_dim = hid_dim
        self.out_dim = out_dim
        self.num_fc_layer = num_fc_layer
        self.num_lstm_layer = num_lstm_layer
        self.hand_size = hand_size
        self.skip_connect = skip_connect

        ff_layers = [nn.Linear(self.in_dim, self.hid_dim), nn.ReLU()]
        for i in range(1, self.num_fc_layer):
            ff_layers.append(nn.Linear(self.hid_dim, self.hid_dim))
            ff_layers.append(nn.ReLU())
        self.net = nn.Sequential(*ff_layers)

        self.lstm = nn.LSTM(
            self.hid_dim, self.hid_dim, num_layers=self.num_lstm_layer,
        ).to(device)
        self.lstm.flatten_parameters()

        self.fc_v = nn.Linear(self.hid_dim, 1)
        self.fc_a = nn.Linear(self.hid_dim, self.out_dim)

        # for aux task
        self.pred = nn.Linear(self.hid_dim, self.hand_size * 3)

    @torch.jit.script_method
    def get_h0(self, batchsize: int) -> Dict[str, torch.Tensor]:
        shape = (self.num_lstm_layer, batchsize, self.hid_dim)
        hid = {"h0": torch.zeros(*shape), "c0": torch.zeros(*shape)}
        return hid

    @torch.jit.script_method
    def act(
        self, priv_s: torch.Tensor, hid: Dict[str, torch.Tensor]
    ) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        assert priv_s.dim() == 2, "dim should be 2, [batch, dim], get %d" % s.dim()

        priv_s = priv_s.unsqueeze(0)
        x = self.net(priv_s)
        o, (h, c) = self.lstm(x, (hid["h0"], hid["c0"]))
        if self.skip_connect:
            o = o + x
        a = self.fc_a(o)
        a = a.squeeze(0)
        return a, {"h0": h, "c0": c}

    @torch.jit.script_method
    def forward(
        self,
        priv_s: torch.Tensor,
        legal_move: torch.Tensor,
        action: torch.Tensor,
        hid: Dict[str, torch.Tensor],
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        assert (
            priv_s.dim() == 3 or priv_s.dim() == 2
        ), "dim = 3/2, [seq_len(optional), batch, dim]"

        one_step = False
        if priv_s.dim() == 2:
            priv_s = priv_s.unsqueeze(0)
            legal_move = legal_move.unsqueeze(0)
            action = action.unsqueeze(0)
            one_step = True

        x = self.net(priv_s)
        if len(hid) == 0:
            o, (h, c) = self.lstm(x)
        else:
            o, (h, c) = self.lstm(x, (hid["h0"], hid["c0"]))
        a = self.fc_a(o)
        v = self.fc_v(o)
        q = self._duel(v, a, legal_move)

        # q: [seq_len, batch, num_action]
        # action: [seq_len, batch]
        qa = q.gather(2, action.unsqueeze(2)).squeeze(2)

        assert q.size() == legal_move.size()
        legal_q = (1 + q - q.min()) * legal_move
        # greedy_action: [seq_len, batch]
        greedy_action = legal_q.argmax(2).detach()

        if one_step:
            qa = qa.squeeze(0)
            greedy_action = greedy_action.squeeze(0)
            o = o.squeeze(0)
            q = q.squeeze(0)
        return qa, greedy_action, q, o

    @torch.jit.script_method
    def _duel(
        self, v: torch.Tensor, a: torch.Tensor, legal_move: torch.Tensor
    ) -> torch.Tensor:
        assert a.size() == legal_move.size()
        legal_a = a * legal_move
        q = v + legal_a - legal_a.mean(2, keepdim=True)
        return q

    def cross_entropy(self, net, lstm_o, target_p, hand_slot_mask, seq_len):
        # target_p: [seq_len, batch, num_player, 5, 3]
        # hand_slot_mask: [seq_len, batch, num_player, 5]
        logit = net(lstm_o).view(target_p.size())
        q = nn.functional.softmax(logit, -1)
        logq = nn.functional.log_softmax(logit, -1)
        plogq = (target_p * logq).sum(-1)
        xent = -(plogq * hand_slot_mask).sum(-1) / hand_slot_mask.sum(-1).clamp(
            min=1e-6
        )

        if xent.dim() == 3:
            # [seq, batch, num_player]
            xent = xent.mean(2)

        # save before sum out
        seq_xent = xent
        xent = xent.sum(0)
        assert xent.size() == seq_len.size()
        avg_xent = (xent / seq_len).mean().item()
        return xent, avg_xent, q, seq_xent.detach()

    def pred_loss_1st(self, lstm_o, target, hand_slot_mask, seq_len):
        return self.cross_entropy(self.pred, lstm_o, target, hand_slot_mask, seq_len)


class R2D2Agent(torch.jit.ScriptModule):
    __constants__ = ["vdn", "multi_step", "gamma", "eta", "uniform_priority"]

    def __init__(
        self,
        vdn,
        multi_step,
        gamma,
        eta,
        device,
        in_dim,
        hid_dim,
        out_dim,
        num_lstm_layer,
        hand_size,
        uniform_priority,
        *,
        num_fc_layer=1,
        skip_connect=False,
    ):
        super().__init__()
        self.online_net = R2D2Net(
            device,
            in_dim,
            hid_dim,
            out_dim,
            num_lstm_layer,
            hand_size,
            num_fc_layer,
            skip_connect,
        ).to(device)
        self.target_net = R2D2Net(
            device,
            in_dim,
            hid_dim,
            out_dim,
            num_lstm_layer,
            hand_size,
            num_fc_layer,
            skip_connect,
        ).to(device)
        self.belief_module = get_model(
            src_vocab = 206,
            trg_vocab = 28,
            d_model = 256,
            N = 6,
            heads = 8
        ).to(device)
        self.vdn = vdn
        self.multi_step = multi_step
        self.gamma = gamma
        self.eta = eta
        self.uniform_priority = uniform_priority
        self.device = device

    @torch.jit.script_method
    def get_h0(self, batchsize: int) -> Dict[str, torch.Tensor]:
        return self.online_net.get_h0(batchsize)

    def clone(self, device, overwrite=None):
        if overwrite is None:
            overwrite = {}
        cloned = type(self)(
            overwrite.get("vdn", self.vdn),
            self.multi_step,
            self.gamma,
            self.eta,
            device,
            self.online_net.in_dim,
            self.online_net.hid_dim,
            self.online_net.out_dim,
            self.online_net.num_lstm_layer,
            self.online_net.hand_size,
            self.uniform_priority,
            num_fc_layer=self.online_net.num_fc_layer,
            skip_connect=self.online_net.skip_connect,
        )
        cloned.load_state_dict(self.state_dict())
        return cloned.to(device)

    def sync_target_with_online(self):
        self.target_net.load_state_dict(self.online_net.state_dict())

    @torch.jit.script_method
    def greedy_act(
        self,
        priv_s: torch.Tensor,
        legal_move: torch.Tensor,
        hid: Dict[str, torch.Tensor],
    ) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        adv, new_hid = self.online_net.act(priv_s, hid)
        legal_adv = (1 + adv - adv.min()) * legal_move
        greedy_action = legal_adv.argmax(1).detach()
        return greedy_action, new_hid

    @torch.jit.script_method
    def act(self, obs: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        """
        Acts on the given obs, with eps-greedy policy.
        output: {'a' : actions}, a long Tensor of shape
            [batchsize] or [batchsize, num_player]
        """
        obsize, ibsize, num_player = 0, 0, 0
        priv_s = obs["priv_s"].detach()

        nopeak_mask = torch.triu(torch.ones((1, 6, 6)), diagonal=1)
        nopeak_mask = (nopeak_mask == 0).to(self.device)

        obsize, ibsize = obs["priv_s"].size()[:2]
        num_player = 1
            
        assert(not self.vdn)
        
        # src is bs x seq_len x 15, where each of the 15 tokens describe a different observable environment feature
        
        src = self.belief_module.get_samples_one_player(obs["aoh"].transpose(0,1).reshape(80,-1,838), obs["own_hand"].reshape(-1, 125), obs["seq_len"].flatten().long(), device=self.device)
        priv_s = priv_s.flatten(0, 1)

        assert(torch.all(0 == torch.sum(priv_s[:,:,0:125], -1)))

        targets = 26 + torch.zeros((src.size(0), 6), dtype=torch.long, device=self.device).detach() # bs x seq_len x 6
        j_card_dist = torch.zeros((src.size(0), 28), dtype=torch.long, device=self.device).detach()
        temp = torch.zeros((src.size(0)), dtype=torch.long, device=self.device).detach()
        for j in range(5):
            while True:
                j_card_dist = F.softmax(self.belief_module(src, targets, None, nopeak_mask)[:,j,:], dim=-1).detach()
                temp = torch.multinomial(j_card_dist, 1)#torch.argmax(torch.log(j_card_dist) + gumbel_dist.sample(sample_shape=j_card_dist.shape).squeeze(-1), axis=1)
                if not torch.any(temp==26) and not torch.any(temp==27):
                    break
            targets[:,j+1] = temp.reshape(src.size(0))
            priv_s[:, 25*j:25*(j+1)] = j_card_dist[:, 0:25]

        legal_move = obs["legal_move"].flatten(0, 1)
        eps = obs["eps"].flatten(0, 1)

        hid = {
            "h0": obs["h0"].flatten(0, 1).transpose(0, 1).contiguous(),
            "c0": obs["c0"].flatten(0, 1).transpose(0, 1).contiguous(),
        }

        greedy_action, new_hid = self.greedy_act(priv_s, legal_move, hid)

        random_action = legal_move.multinomial(1).squeeze(1)
        rand = torch.rand(greedy_action.size(), device=greedy_action.device)
        assert rand.size() == eps.size()
        rand = (rand < eps).long()
        action = (greedy_action * (1 - rand) + random_action * rand).detach().long()

        if self.vdn:
            action = action.view(obsize, ibsize, num_player)
            greedy_action = greedy_action.view(obsize, ibsize, num_player)
            rand = rand.view(obsize, ibsize, num_player)
        else:
            action = action.view(obsize, ibsize)
            greedy_action = greedy_action.view(obsize, ibsize)
            rand = rand.view(obsize, ibsize)

        hid_shape = (
            obsize,
            ibsize * num_player,
            self.online_net.num_lstm_layer,
            self.online_net.hid_dim,
        )
        h0 = new_hid["h0"].transpose(0, 1).view(*hid_shape)
        c0 = new_hid["c0"].transpose(0, 1).view(*hid_shape)

        reply = {
            "a": action.detach().cpu(),
            "greedy_a": greedy_action.detach().cpu(),
            "h0": h0.contiguous().detach().cpu(),
            "c0": c0.contiguous().detach().cpu(),
        }
        return reply

    @torch.jit.script_method
    def compute_priority(
        self, input_: Dict[str, torch.Tensor]
    ) -> Dict[str, torch.Tensor]:
        """
        compute priority for one batch
        """
        # uniform priority:
        return {"priority": torch.ones((input_["priv_s"].size(0), input_["priv_s"].size(1)))}

    ############# python only functions #############
    def flat_4d(self, data):
        """
        rnn_hid: [num_layer, batch, num_player, dim] -> [num_player, batch, dim]
        seq_obs: [seq_len, batch, num_player, dim] -> [seq_len, batch, dim]
        """
        bsize = 0
        num_player = 0
        for k, v in data.items():
            if num_player == 0:
                bsize, num_player = v.size()[1:3]

            if v.dim() == 4:
                d0, d1, d2, d3 = v.size()
                data[k] = v.view(d0, d1 * d2, d3)
            elif v.dim() == 3:
                d0, d1, d2 = v.size()
                data[k] = v.view(d0, d1 * d2)
        return bsize, num_player

    def td_error(self, obs, hid, action, reward, terminal, bootstrap, seq_len, stat):
        with torch.no_grad():
            # start_time = time.time()
            max_seq_len = obs["priv_s"].size(0)

            #if self.belief_module.use:

            nopeak_mask = torch.triu(torch.ones((1, 6, 6)), diagonal=1)
            nopeak_mask = (nopeak_mask == 0).to("cuda:0").detach()

            src = self.belief_module.get_samples_one_player(obs["priv_s"].detach(),
                                                                    obs["own_hand"].detach(),
                                                                    seq_len.detach(),
                                                                    device="cuda:0")
            src = src.detach()

            priv_s = obs["priv_s"].detach()
            
            targets = 26 + torch.zeros((src.size(0), 6), dtype=torch.long, device="cuda:0").detach() # bs x seq_len x 6
            j_card_dist = torch.zeros((src.size(0), 28), dtype=torch.long, device="cuda:0").detach()
            temp = torch.zeros((src.size(0)), dtype=torch.long, device="cuda:0").detach()

            assert(torch.all(0 == torch.sum(priv_s[:,:,0:125], -1)))

            for j in range(5):
                while True:
                    j_card_dist = F.softmax(self.belief_module(src, targets, None, nopeak_mask)[:,j,:], dim=-1).detach()
                    temp = torch.multinomial(j_card_dist, 1)#torch.argmax(torch.log(j_card_dist) + gumbel_dist.sample(sample_shape=j_card_dist.shape).squeeze(-1), axis=1)
                    if not torch.any(temp==26) and not torch.any(temp==27):
                        break
                targets[:,j+1] = temp.reshape(src.size(0))
                for i in range(src.size(0)):
                    priv_s[0:seq_len[i], i, 25*j:25*(j+1)] = j_card_dist[i, 0:25]

            bsize, num_player = 0, 1
            if self.vdn:
                bsize, num_player = self.flat_4d(obs)
                self.flat_4d(action)
                priv_s = priv_s.reshape(priv_s.size(0), 2*priv_s.size(1), -1).detach()

            legal_move = obs["legal_move"].detach()
            action = action["a"].detach()

        hid = {}

        # this only works because the trajectories are padded,
        # i.e. no terminal in the 
        online_qa, greedy_a, _, lstm_o = self.online_net(
            priv_s, legal_move, action, hid
        )

        with torch.no_grad():
            target_qa, _, _, _ = self.target_net(priv_s, legal_move, greedy_a, hid)
            # assert target_q.size() == pa.size()
            # target_qe = (pa * target_q).sum(-1).detach()
            assert online_qa.size() == target_qa.size()

        if self.vdn:
            online_qa = online_qa.view(max_seq_len, bsize, num_player).sum(-1)
            target_qa = target_qa.view(max_seq_len, bsize, num_player).sum(-1)
            lstm_o = lstm_o.view(max_seq_len, bsize, num_player, -1)

        terminal = terminal.float()
        bootstrap = bootstrap.float()

        errs = []
        target_qa = torch.cat(
            [target_qa[self.multi_step :], target_qa[: self.multi_step]], 0
        )
        target_qa[-self.multi_step :] = 0

        assert target_qa.size() == reward.size()
        target = reward + bootstrap * (self.gamma ** self.multi_step) * target_qa
        mask = torch.arange(0, max_seq_len, device=seq_len.device)
        mask = (mask.unsqueeze(1) < seq_len.unsqueeze(0)).float()
        err = (target.detach() - online_qa) * mask
        # print("td time: " + str(time.time()-start_time))
        return err, lstm_o#, belief_losses

    def aux_task_iql(self, lstm_o, hand, seq_len, rl_loss_size, stat):
        seq_size, bsize, _ = hand.size()
        own_hand = hand.view(seq_size, bsize, self.online_net.hand_size, 3)
        own_hand_slot_mask = own_hand.sum(3)
        pred_loss1, avg_xent1, _, _ = self.online_net.pred_loss_1st(
            lstm_o, own_hand, own_hand_slot_mask, seq_len
        )
        assert pred_loss1.size() == rl_loss_size

        stat["aux1"].feed(avg_xent1)
        return pred_loss1

    def aux_task_vdn(self, lstm_o, hand, t, seq_len, rl_loss_size, stat):
        """1st and 2nd order aux task used in VDN"""
        seq_size, bsize, num_player, _ = hand.size()
        own_hand = hand.view(seq_size, bsize, num_player, self.online_net.hand_size, 3)
        own_hand_slot_mask = own_hand.sum(4)
        pred_loss1, avg_xent1, belief1, _ = self.online_net.pred_loss_1st(
            lstm_o, own_hand, own_hand_slot_mask, seq_len
        )
        assert pred_loss1.size() == rl_loss_size

        rotate = [num_player - 1]
        rotate.extend(list(range(num_player - 1)))
        partner_hand = own_hand[:, :, rotate, :, :]
        partner_hand_slot_mask = partner_hand.sum(4)
        partner_belief1 = belief1[:, :, rotate, :, :].detach()

        stat["aux1"].feed(avg_xent1)
        return pred_loss1

    def loss(self, batch, pred_weight, stat):
        err, lstm_o = self.td_error(
            batch.obs,
            batch.h0,
            batch.action,
            batch.reward,
            batch.terminal,
            batch.bootstrap,
            batch.seq_len.to(torch.long),
            stat,
        )
        rl_loss = nn.functional.smooth_l1_loss(
            err, torch.zeros_like(err), reduction="none"
        )
        rl_loss = rl_loss.sum(0)
        stat["rl_loss"].feed((rl_loss / batch.seq_len).mean().item())

        priority = err.abs()
        # priority = self.aggregate_priority(p, batch.seq_len)

        if pred_weight > 0:
            if self.vdn:
                pred_loss1 = self.aux_task_vdn(
                    lstm_o,
                    batch.obs["own_hand"],
                    batch.obs["temperature"],
                    batch.seq_len,
                    rl_loss.size(),
                    stat,
                )
                loss = rl_loss + pred_weight * pred_loss1
            else:
                pred_loss = self.aux_task_iql(
                    lstm_o, batch.obs["own_hand"], batch.seq_len, rl_loss.size(), stat,
                )
                loss = rl_loss + pred_weight * pred_loss
        else:
            loss = rl_loss
        return loss, priority#, belief_losses
