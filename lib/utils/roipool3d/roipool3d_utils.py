# import torch
# import roipool3d_cuda
import numpy as np
import lib.utils.kitti_utils as kitti_utils
import mindspore as ms
import mindspore.nn as nn
from mindspore import ops
from pathlib import Path
import os, sys
from pathlib import Path

sys.path.insert(0,
                Path(__file__).absolute().parent.parent.parent.parent.parent.absolute())
from tools.layer_utils import get_func_from_so, log_to_file

so_name = "roipool3d_cuda.cpython-39-x86_64-linux-gnu.so"


def roipool3d_gpu(pts,
                  pts_feature,
                  boxes3d,
                  pool_extra_width,
                  sampled_pt_num=512):
    """
    :param pts: (B, N, 3)
    :param pts_feature: (B, N, C)
    :param boxes3d: (B, M, 7)
    :param pool_extra_width: float
    :param sampled_pt_num: int
    :return:
        pooled_features: (B, M, 512, 3 + C)
        pooled_empty_flag: (B, M)
    """
    batch_size, boxes_num, feature_len = pts.shape[0], boxes3d.shape[
        1], pts_feature.shape[2]
    pooled_boxes3d = kitti_utils.enlarge_box3d(boxes3d.view(-1, 7),
                                               pool_extra_width).view(
                                                   batch_size, -1, 7)

    # pooled_features = torch.cuda.FloatTensor(torch.Size((batch_size, boxes_num,sampled_pt_num, 3 + feature_len))).zero_()
    # pooled_features = ms.numpy.zeros((batch_size, boxes_num,sampled_pt_num, 3 + feature_len), dtype=ms.numpy.float32)
    # pooled_empty_flag = torch.cuda.IntTensor(torch.Size((batch_size, boxes_num))).zero_()
    # pooled_empty_flag = ms.numpy.zeros((batch_size, boxes_num), dtype=ms.numpy.int32)

    # roipool3d_cuda.forward(pts.contiguous(), pooled_boxes3d.contiguous(),
    #                        pts_feature.contiguous(), pooled_features, pooled_empty_flag)
    # roipool3d_cuda.forward(pts, pooled_boxes3d, pts_feature, pooled_features, pooled_empty_flag)

    forward = get_func_from_so(so_name,
                               "roipool3d_gpu",
                               out_shape=((batch_size, boxes_num,
                                           sampled_pt_num, 3 + feature_len),
                                          (batch_size, boxes_num)),
                               out_dtype=(ms.float32, ms.int32))
    pooled_features, pooled_empty_flag = forward(pts, pooled_boxes3d,
                                                 pts_feature)
    return pooled_features, pooled_empty_flag


boxes3d_op = get_func_from_so(so_name,
                              "pts_in_boxes3d_cpu",
                              out_shape=lambda *x: x[0],
                              out_dtype=ms.int64,
                              CPU_opt=True)


def pts_in_boxes3d_cpu(pts: ms.Tensor, boxes3d: ms.Tensor):
    """
    :param pts: (N, 3) in rect-camera coords
    :param boxes3d: (M, 7)
    :return: boxes_pts_mask_list: (M), list with [(N), (N), ..]
    """
    # pts = pts.float().contiguous()
    pts = pts.astype(ms.float32)
    # boxes3d = boxes3d.float().contiguous()
    boxes3d = boxes3d.astype(ms.float32)
    # pts_flag = torch.LongTensor(torch.Siz((boxes3d.size(0), pts.size(0))))  # (M, N)
    _pts_flag = ms.numpy.zeros((boxes3d.shape[0], pts.shape[0]),
                               dtype=ms.numpy.int64)
    # print(111)
    # roipool3d_cuda.pts_in_boxes3d_cpu(pts_flag,pts, boxes3d)

    pts_flag = boxes3d_op(_pts_flag, pts, boxes3d)
    boxes_pts_mask_list = []
    for k in range(0, boxes3d.shape[0]):
        cur_mask = pts_flag[k] > 0
        boxes_pts_mask_list.append(cur_mask)

    # 打桩
    # return ms.numpy.randint(1,10,(boxes3d.shape[0], pts.shape[0]), dtype=ms.numpy.int32)
    return boxes_pts_mask_list


def roipool_pc_cpu(pts, pts_feature, boxes3d, sampled_pt_num):
    """
    :param pts: (N, 3)
    :param pts_feature: (N, C)
    :param boxes3d: (M, 7)
    :param sampled_pt_num: int
    :return:
    """
    # pts = pts.cpu().float().contiguous()
    pts = pts.astype(ms.float32)
    # pts_feature = pts_feature.cpu().float().contiguous()
    pts_feature = pts_feature.astype(ms.float32)
    # boxes3d = boxes3d.cpu().float().contiguous()
    boxes3d = boxes3d.astype(ms.float32)
    assert pts.shape[0] == pts_feature.shape[0] and pts.shape[
        1] == 3, '%s %s' % (pts.shape, pts_feature.shape)
    # assert pts.is_cuda is False
    # pooled_pts = torch.FloatTensor(torch.Size((boxes3d.shape[0], sampled_pt_num, 3))).zero_()
    # pooled_pts = ms.numpy.zeros((boxes3d.shape[0], sampled_pt_num, 3),dtype=ms.numpy.float32)
    out1 = (boxes3d.shape[0], sampled_pt_num, 3)
    # pooled_features = torch.FloatTensor(torch.Size((boxes3d.shape[0], sampled_pt_num, pts_feature.shape[1]))).zero_()
    # pooled_features = ms.numpy.zeros((boxes3d.shape[0], sampled_pt_num, pts_feature.shape[1]),dtype=ms.numpy.float32)
    out2 = (boxes3d.shape[0], sampled_pt_num, pts_feature.shape[1])
    # pooled_empty_flag = torch.LongTensor(boxes3d.shape[0]).zero_()
    # pooled_empty_flag = ms.numpy.zeros(boxes3d.shape[0], dtype=ms.numpy.int64)
    out3 = boxes3d.shape[0]
    # roipool3d_cuda.roipool3d_cpu(pts, boxes3d, pts_feature, pooled_pts, pooled_features, pooled_empty_flag)
    roipool3d_cpu_op = get_func_from_so(so_name, "roipool3d_cpu", out_shape=(out1,out2,out3),out_dtype=(ms.float32,ms.float32,ms.int64))
    # roipool3d_cpu(pts, boxes3d, pts_feature, pooled_pts, pooled_features,pooled_empty_flag)
    pooled_pts, pooled_features,pooled_empty_flag = roipool3d_cpu_op(pts, boxes3d, pts_feature)
    return pooled_pts, pooled_features, pooled_empty_flag


def roipool3d_cpu(boxes3d,
                  pts,
                  pts_feature,
                  pts_extra_input,
                  pool_extra_width,
                  sampled_pt_num=512,
                  canonical_transform=True):
    """
    :param boxes3d: (N, 7)
    :param pts: (N, 3)
    :param pts_feature: (N, C)
    :param pts_extra_input: (N, C2)
    :param pool_extra_width: constant
    :param sampled_pt_num: constant
    :return:
    """
    pooled_boxes3d = kitti_utils.enlarge_box3d(boxes3d, pool_extra_width)

    pts_feature_all = np.concatenate((pts_extra_input, pts_feature), axis=1)

    #  Note: if pooled_empty_flag[i] > 0, the pooled_pts[i], pooled_features[i] will be zero
    # pooled_pts, pooled_features, pooled_empty_flag = \
    #     roipool_pc_cpu(torch.from_numpy(pts), torch.from_numpy(pts_feature_all),
    #                    torch.from_numpy(pooled_boxes3d), sampled_pt_num)
    pooled_pts, pooled_features, pooled_empty_flag = roipool_pc_cpu(
        ms.Tensor.from_numpy(pts), ms.Tensor.from_numpy(pts_feature_all),
        ms.Tensor.from_numpy(pooled_boxes3d), sampled_pt_num)

    extra_input_len = pts_extra_input.shape[1]
    # ms.Tensor.asnumpy()
    sampled_pts_input = ms.ops.concat(
        (pooled_pts, pooled_features[:, :, 0:extra_input_len]),
        axis=2).asnumpy()
    sampled_pts_feature = pooled_features[:, :, extra_input_len:].asnumpy()

    if canonical_transform:
        # Translate to the roi coordinates
        roi_ry = boxes3d[:, 6] % (2 * np.pi)  # 0~2pi
        roi_center = boxes3d[:, 0:3]

        # shift to center
        sampled_pts_input[:, :,
                          0:3] = sampled_pts_input[:, :, 0:
                                                   3] - roi_center[:, np.
                                                                   newaxis, :]
        for k in range(sampled_pts_input.shape[0]):
            sampled_pts_input[k] = kitti_utils.rotate_pc_along_y(
                sampled_pts_input[k], roi_ry[k])

        return sampled_pts_input, sampled_pts_feature

    return sampled_pts_input, sampled_pts_feature, pooled_empty_flag.asnumpy()


if __name__ == '__main__':
    pass
