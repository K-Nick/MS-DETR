# ============================================================================ #
# * use reference centers from output of encoder as the initial referecence of decoder querys
# ============================================================================ #

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import einsum, repeat, rearrange, reduce
from kn_util.basic import registry
from ..backbone.segformerx import SegFormerXFPN, SegFormerX
from .ms_pooler import MultiScaleRoIAlign1D
from misc import cw2se, calc_iou
from ..loss import l1_loss, iou_loss
from kn_util.nn_utils.layers import MLP
from kn_util.nn_utils import clones
from kn_util.nn_utils.math import inverse_sigmoid_torch, gaussian_torch
from kn_util.basic import registry, global_get
from torchvision.ops import sigmoid_focal_loss
from kn_util.nn_utils.init import init_module
import os

NUM_GROUP = 3

class QueryBasedDecoder(nn.Module):

    def __init__(self,
                 d_model,
                 nhead,
                 ff_dim,
                 num_query,
                 num_layers=4,
                 num_scales=4,
                 pooler_resolution=16,
                 dim_init_ref=1,
                 dropout=0.1) -> None:
        super().__init__()

        num_group = global_get("cfg").model_cfg.num_group
        self.query_embeddings = clones(nn.Embedding(num_query, d_model), num_group)
        self.d_model = d_model

        bbox_head = MLP([d_model, d_model, 2])
        self.bbox_heads = clones(bbox_head, num_layers)
        self.reference_head = MLP([d_model, d_model, dim_init_ref])
        score_head = MLP([d_model, d_model, 1])
        self.score_heads = clones(score_head, num_layers)

        vocab = global_get("glove_vocab")
        vocab_size = 1513 if vocab is None else len(vocab[0])

        self.num_layers = num_layers
        # make sure first offset is reasonable

        layer = nn.TransformerDecoderLayer(d_model, nhead, ff_dim, dropout=dropout, batch_first=True)
        self.layers = clones(layer, num_layers)

        # build pooler
        self.pooler = MultiScaleRoIAlign1D(output_size=pooler_resolution)

        # prepare for processing pooled_feat
        pool_ffn = MLP([d_model * pooler_resolution * num_scales,\
                        d_model, d_model])
        self.pool_ffns = clones(pool_ffn, num_layers)

        self.init_weight()

    def init_weight(self):
        self.apply(init_module)
        for bbox_head in self.bbox_heads:
            nn.init.constant_(bbox_head.layers[-1].weight, 0.0)
            nn.init.constant_(bbox_head.layers[-1].bias, 0.0)

        # TEST bbox bias init
        cfg = global_get("cfg")
        if hasattr(cfg.model_cfg, "bbox_bias_init"):
            nn.init.constant_(self.bbox_heads[0].layers[-1].bias[1:], cfg.model_cfg.bbox_bias_init)
        else:
            nn.init.constant_(self.bbox_heads[0].layers[-1].bias[1:], -2.0)
            # nn.init.normal_(self.bbox_heads[0], mean=-2, std=1)

    def get_initital_reference(self, offset, reference_no_sigmoid):
        # assume temporal length of first prediction totally depends on offset predicted by boxx_head
        offset[..., :1] += reference_no_sigmoid.unsqueeze(-1)

        return offset

    def forward(self, feat_lvls, reference_centers):
        B = feat_lvls[0].shape[0]
        model_cfg = global_get("cfg").model_cfg
        Ng = model_cfg.num_group if self.training else 1

        proposal_lvls_group = []
        score_lvls_group = []

        query_embeddings_cat = torch.stack([self.query_embeddings[i].weight for i in range(Ng)], dim=0)

        memory = feat_lvls[-1]
        tgt = repeat(query_embeddings_cat, "ng nq d -> (b ng) nq d", b=B)
        expanded_memory = repeat(memory, "b t d -> (b ng) t d", g = Ng)
        reference = None
        proposal_lvls = []
        score_lvls = []

        for idx, layer in enumerate(self.layers):
            output = layer(tgt, expanded_memory)  # (B Ng), Nq, D

            # box score
            score_logits = self.score_heads[idx](output)
            score = score_logits.squeeze(-1)
            score_lvls.append(score)

            # box refine / get initial box
            offset = self.bbox_heads[idx](output)
            reference_no_sigmoid = inverse_sigmoid_torch(reference_centers, eps=1e-12)
            if idx == 0:
                offset = self.get_initital_reference(offset, reference_no_sigmoid)
            else:
                offset = inverse_sigmoid_torch(reference) + offset
            reference = offset.sigmoid()
            proposal = cw2se(reference, fix_out_of_bound=not self.training)  # (B Ng), Nq, 2
            proposal_lvls.append(proposal)

            proposal = proposal.detach()
            import ipdb; ipdb.set_trace() #FIXME ipdb

            # roi pool & concat
            pooled_feat_list = self.pooler(feat_lvls, proposal)
            pooled_feat_list = [torch.stack(x, dim=0) for x in pooled_feat_list]
            pooled_feat = torch.stack(pooled_feat_list, dim=0)
            # B, N_lvls, Nq, Rp, D
            pooled_feat = rearrange(pooled_feat, "nlvl b nq rd d -> b nq (nlvl rd d)")

            pooled_feat = self.pool_ffns[idx](pooled_feat)
            tgt = pooled_feat + query_embeddings

            proposal_lvls_group.append(proposal_lvls)
            score_lvls_group.append(score_lvls)

            if not self.training:
                break  # only use first group

        return proposal_lvls_group, score_lvls_group


class MultiScaleTemporalDetr(nn.Module):

    def __init__(self, backbone, head, frame_pooler, model_cfg) -> None:
        super().__init__()
        self.backbone = backbone
        self.head = head
        self.model_cfg = model_cfg
        self.frame_pooler = frame_pooler

        d_model = model_cfg.d_model

        # stage mlp
        self.stage_mlp1 = nn.Linear(d_model, 3 * d_model)
        self.stage_mlps = clones(MLP([d_model, d_model, 1]), 3)

        vocab_size = global_get("vocab_size")
        self.text_mlp = MLP([d_model, d_model, vocab_size])

        self.stage_mlp1.apply(init_module)
        self.stage_mlps.apply(init_module)
        self.text_mlp.apply(init_module)

    def compute_boudary_loss(self, proposal, score, gt, idx):
        cfg = self.model_cfg
        loss = 0.0

        B, Nc, _2 = proposal.shape

        if os.getenv("KN_USE_IOU", False):  # TEST use IOU instead of giou
            iou_gt_flatten = calc_iou(proposal.reshape(B * Nc, 2),
                                      gt[:, None].repeat(1, Nc, 1).reshape(B * Nc, 2),
                                      type="iou")
        else:
            iou_gt_flatten = calc_iou(proposal.reshape(B * Nc, 2),
                                      gt[:, None].repeat(1, Nc, 1).reshape(B * Nc, 2),
                                      type="giou")
        iou_gt = iou_gt_flatten.reshape((B, Nc))

        if os.getenv("KN_HYBRID_MATCH", False):  # TEST hybrid matching
            if self.model_cfg.num_layers_dec == 6:
                topk_list = self.model_cfg.topk_list
            else:
                topk_list = self.model_cfg.topk_list
            cfg.topk = topk_list[idx]

        topk_indices_flatten = torch.topk(iou_gt, k=cfg.topk, dim=1).indices.flatten()
        batch_indices_flatten = torch.arange(0, B, device=proposal.device)[:, None].repeat(1, cfg.topk).flatten()
        # iou_gt[iou_gt < 0.5] = 0.0
        iou_gt[iou_gt < cfg.iou_cutoff] = 0.0
        iou_gt[batch_indices_flatten, topk_indices_flatten] = 1.0

        # TEST iou soft
        if os.getenv("KN_IOU_SOFT", False):
            score = score[batch_indices_flatten, topk_indices_flatten]
            iou_gt = iou_gt[batch_indices_flatten, topk_indices_flatten]

        val_iou_loss = sigmoid_focal_loss(score.flatten(), iou_gt.flatten(), reduction="mean")
        loss += val_iou_loss * cfg.w_iou_loss

        topk_proposal = proposal[batch_indices_flatten, topk_indices_flatten].reshape(B, cfg.topk, 2)

        val_l1_loss = l1_loss(topk_proposal, gt)
        loss += val_l1_loss * cfg.w_l1_loss

        return dict(loss=loss, l1_loss=val_l1_loss, iou_loss=val_iou_loss, topk_indices_flatten=topk_indices_flatten)

    def compute_stage_loss(self, stage_logits, map_gt):
        map_gt = map_gt[:, :3, :].transpose(1, 2)
        stage_loss = sigmoid_focal_loss(stage_logits, map_gt, reduction="mean", alpha=0.5)
        return stage_loss

    def compute_iou_loss(self, proposal, score, gt):
        # calc_iou(gt)
        Nc = proposal.shape[1]
        expanded_gt = repeat(gt, "b i -> (b nc) i", nc=Nc)
        proposal = rearrange(proposal, "b nc i -> (b nc) i", nc=Nc)
        iou_type = "giou" if os.getenv("KN_SOFT_GIOU", False) else "iou"
        ious = calc_iou(proposal, expanded_gt, type=iou_type)
        score_flatten = score.flatten()
        return sigmoid_focal_loss(score_flatten, ious, alpha=0.5, reduction="mean")

    def compute_span_loss(self, stage_logits, gt):
        bsz = gt.shape[0]
        num_clips = self.model_cfg.num_clips
        map_gt = torch.zeros(bsz, num_clips, device=stage_logits.device, dtype=torch.float)
        gt_idxs = torch.round(gt * (num_clips - 1)).long()

        range_idxs = torch.arange(num_clips, device=stage_logits.device)
        map_gt = torch.logical_and(range_idxs[None,] >= gt_idxs[:, :1], range_idxs[None,] <= gt_idxs[:, 1:])
        map_gt = map_gt.float()

        return sigmoid_focal_loss(stage_logits[..., 0], map_gt, alpha=0.5, reduction="mean")

    def compute_loss(self, proposal_lvls_group, score_lvls_group, stage_logits, text_logits, word_mask, word_label, gt):
        cfg = self.model_cfg
        loss = 0.0
        loss_dict = dict()

        # TEST iou soft
        # let score predicting precise iou
        if os.getenv("KN_IOU_SOFT", False):
            assert hasattr(cfg, "w_iou_loss")
            for idx, (proposal, score) in enumerate(zip(proposal_lvls, score_lvls)):
                iou_loss = self.compute_iou_loss(proposal, score, gt)
                loss += iou_loss * cfg.w_iou_loss
                loss_dict[f"iou_{idx}_loss"] = iou_loss

        # decoder boundary loss
        all_bd_loss = []
        for group_idx in range(len(proposal_lvls_group)):
            for idx, (proposal, score) in enumerate(zip(proposal_lvls_group[group_idx], score_lvls_group[group_idx])):
                bd_loss_dict = self.compute_boudary_loss(proposal, score, gt, idx=idx)
                bd_loss = bd_loss_dict["loss"]
                all_bd_loss.append(bd_loss)
                loss += bd_loss * cfg.w_bd_loss[idx] / 5
                loss_dict[f"bd_{idx}_gr{group_idx}_loss"] = bd_loss

        # stage loss
        span_loss = self.compute_span_loss(stage_logits, gt)
        assert hasattr(cfg, "w_span_loss")
        loss += span_loss * cfg.w_span_loss
        loss_dict[f"span_loss"] = span_loss

        # text loss
        mask_loss = F.cross_entropy(text_logits.transpose(1, 2), word_label, reduction="none") * word_mask.float()
        mask_loss = mask_loss.mean()
        loss_dict["mask_loss"] = mask_loss
        loss += cfg.w_mask_loss * mask_loss

        loss_dict["loss"] = loss

        return loss_dict

    def forward(self, vid_feat, txt_feat, txt_mask, word_mask=None, word_label=None, gt=None, mode="train", **kwargs):
        B = vid_feat.shape[0]

        model_cfg = self.model_cfg
        vid_feat = self.frame_pooler(vid_feat)
        # word_mask = torch.zeros(txt_mask.shape, dtype=torch.bool, device=vid_feat.device)
        vid_feat_lvls, txt_feat = self.backbone(vid_feat=vid_feat,
                                                txt_feat=txt_feat,
                                                txt_mask=txt_mask,
                                                word_mask=word_mask)

        vid_feat_stage = self.stage_mlp1(vid_feat_lvls[-1])
        hidden_st, hidden_ed, hidden_md = torch.split(vid_feat_stage, model_cfg.d_model, dim=-1)
        logits_st = self.stage_mlps[0](hidden_st)
        logits_ed = self.stage_mlps[1](hidden_ed)
        logits_md = self.stage_mlps[2](hidden_md)
        stage_logits = torch.cat([logits_st, logits_ed, logits_md], dim=-1)

        # if not os.getenv("KN_ENC_DISABLE_TOPK", False):  # TEST disable topk encoder
        #     topk_indices = torch.topk(stage_logits[..., -1], k=model_cfg.num_query, dim=1).indices
        # else:
        topk_indices = torch.arange(0, model_cfg.num_query, device=vid_feat.device)
        topk_indices = repeat(topk_indices, "i -> b i", b=B)
        reference_centers = topk_indices / model_cfg.num_query
        reference_centers = reference_centers.detach()

        proposal_lvls_group, score_lvls_group = self.head(vid_feat_lvls, reference_centers)

        text_logits = self.text_mlp(txt_feat)

        # ========= DEBUG ===========
        def print_debug():
            print(f"gt: {gt}")
            for idx, (proposal, score) in enumerate(zip(proposal_lvls_group[0], score_lvls_group[0])):
                topk_indices = torch.topk(score, k=5, dim=1).indices
                topk_proposal = proposal[0, topk_indices]
                topk_score = score[0, topk_indices]
                print(f"======= proposal {idx} ======")
                print(
                    list(
                        zip(topk_proposal.detach().cpu().numpy().tolist()[0],
                            topk_score.detach().cpu().numpy().tolist()[0])))

            import ipdb
            ipdb.set_trace()  #FIXME ipdb

        if os.getenv("KN_DEBUG", False):
            print_debug()

        if mode in ("train", "test"):
            losses = self.compute_loss(proposal_lvls_group, score_lvls_group, stage_logits, text_logits, word_mask,
                                       word_label, gt)
            if mode == "test":
                losses.update(dict(scores=score_lvls_group[0][-1], boxxes=proposal_lvls_group[0][-1]),
                              reference_centers=reference_centers,
                              stage_logits=stage_logits)
            return losses
        elif mode == "inference":
            return dict(scores=score_lvls_group[0][-1], boxxes=proposal_lvls_group[0][-1])
