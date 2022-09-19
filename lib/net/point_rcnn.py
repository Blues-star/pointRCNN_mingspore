# import torch
# import torch.nn as nn
from lib.net.rpn import RPN
from lib.net.rcnn_net import RCNNNet
from lib.config import cfg

import mindspore as ms
from mindspore import nn,ops


class PointRCNN(nn.Cell):
    def __init__(self, num_classes, use_xyz=True, mode='TRAIN'):
        super(PointRCNN,self).__init__()
        self.training = (mode=='TRAIN')
        assert cfg.RPN.ENABLED or cfg.RCNN.ENABLED

        if cfg.RPN.ENABLED:
            self.rpn = RPN(use_xyz=use_xyz, mode=mode)

        if cfg.RCNN.ENABLED:
            rcnn_input_channels = 128  # channels of rpn features
            if cfg.RCNN.BACKBONE == 'pointnet':
                self.rcnn_net = RCNNNet(num_classes=num_classes, input_channels=rcnn_input_channels, use_xyz=use_xyz,mode=mode)
            elif cfg.RCNN.BACKBONE == 'pointsift':
                pass 
            else:
                raise NotImplementedError

    def construct(self, input_data):
        if cfg.RPN.ENABLED:
            output = {}
            # rpn inference
            rpn_output = self.rpn(input_data)
            output.update(rpn_output)

            # rcnn inference
            if cfg.RCNN.ENABLED:
                # with torch.no_grad():
                rpn_cls, rpn_reg = rpn_output['rpn_cls'], rpn_output['rpn_reg']
                backbone_xyz, backbone_features = rpn_output['backbone_xyz'], rpn_output['backbone_features']
                rpn_scores_raw = rpn_cls[:, :, 0]
                
                rpn_scores_norm = ops.Sigmoid()(rpn_scores_raw)
                seg_mask = (rpn_scores_norm > cfg.RPN.SCORE_THRESH).astype(ms.float32)
                pts_depth = ops.norm(backbone_xyz, axis=2, p=2)
                # pts_depth = torch.norm(backbone_xyz, p=2, dim=2)
                # proposal layer
                rois, roi_scores_raw = self.rpn.proposal_layer(rpn_scores_raw, rpn_reg, backbone_xyz)  # (B, M, 7)
                output['rois'] = rois
                output['roi_scores_raw'] = roi_scores_raw
                output['seg_result'] = seg_mask

                # ms.Tensor.transpose((0, 2, 1))
                rcnn_input_info = {'rpn_xyz': backbone_xyz,
                                   'rpn_features': backbone_features.transpose((0, 2, 1)),
                                   'seg_mask': seg_mask,
                                   'roi_boxes3d': rois,
                                   'pts_depth': pts_depth}
                if self.training:
                    rcnn_input_info['gt_boxes3d'] = input_data['gt_boxes3d']

                rcnn_output = self.rcnn_net(**rcnn_input_info)
                output.update(rcnn_output)

        elif cfg.RCNN.ENABLED:
            output = self.rcnn_net(input_data)
        else:
            raise NotImplementedError

        return output



