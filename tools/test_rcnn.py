# import _init_path
# import torch
# import torch.optim as optim
# import torch.optim.lr_scheduler as lr_sched
# import torch.nn as nn
# from torch.utils.data import DataLoader
import _init_path
import mindspore as ms
from mindspore import Tensor, nn, ops, Model
from mindspore import load_checkpoint, load_param_into_net
import numpy as np
# from tensorboardX import SummaryWriter
import os
import argparse
import logging
from functools import partial
from datautil import create_dataloader

from lib.net.point_rcnn import PointRCNN
import lib.net.train_functions as train_functions
from lib.datasets.kitti_rcnn_dataset import KittiRCNNDataset
from lib.config import cfg, cfg_from_file, save_config_to_file
# import tools.train_utils.train_utils as train_utils
from tools.train_utils.fastai_optim import OptimWrapper
# from tools.train_utils import learning_schedules_fastai as lsf
from lib.net.ms_loss import net_with_loss

from mindspore import context,Callback
from mindspore.train.callback import ModelCheckpoint, CheckpointConfig, LossMonitor, TimeMonitor
ms.context.set_context(device_target="GPU")
ms.context.set_context(mode=ms.PYNATIVE_MODE,pynative_synchronize=True)

parser = argparse.ArgumentParser(description="arg parser")
parser.add_argument('--cfg_file',
                    type=str,
                    default='cfgs/default.yaml',
                    help='specify the config for training')
parser.add_argument("--train_mode",
                    type=str,
                    default='rpn',
                    required=True,
                    help="specify the training mode")
parser.add_argument("--batch_size",
                    type=int,
                    default=16,
                    required=True,
                    help="batch size for training")
parser.add_argument("--epochs",
                    type=int,
                    default=200,
                    required=True,
                    help="Number of epochs to train for")

parser.add_argument('--workers',
                    type=int,
                    default=8,
                    help='number of workers for dataloader')
parser.add_argument("--ckpt_save_interval",
                    type=int,
                    default=5,
                    help="number of training epochs")
parser.add_argument('--output_dir',
                    type=str,
                    default=None,
                    help='specify an output directory if needed')
parser.add_argument('--mgpus',
                    action='store_true',
                    default=False,
                    help='whether to use multiple gpu')

parser.add_argument("--ckpt",
                    type=str,
                    default=None,
                    help="continue training from this checkpoint")
parser.add_argument("--rpn_ckpt",
                    type=str,
                    default=None,
                    help="specify the well-trained rpn checkpoint")

parser.add_argument("--gt_database",
                    type=str,
                    default='gt_database/train_gt_database_3level_Car.pkl',
                    help='generated gt database for augmentation')
parser.add_argument(
    "--rcnn_training_roi_dir",
    type=str,
    default=None,
    help='specify the saved rois for rcnn training when using rcnn_offline mode'
)
parser.add_argument(
    "--rcnn_training_feature_dir",
    type=str,
    default=None,
    help=
    'specify the saved features for rcnn training when using rcnn_offline mode'
)

parser.add_argument('--train_with_eval',
                    action='store_true',
                    default=False,
                    help='whether to train with evaluation')
parser.add_argument(
    "--rcnn_eval_roi_dir",
    type=str,
    default=None,
    help=
    'specify the saved rois for rcnn evaluation when using rcnn_offline mode')
parser.add_argument(
    "--rcnn_eval_feature_dir",
    type=str,
    default=None,
    help=
    'specify the saved features for rcnn evaluation when using rcnn_offline mode'
)
args = parser.parse_args()


def create_logger(log_file):
    log_format = '%(asctime)s  %(levelname)5s  %(message)s'
    logging.basicConfig(level=logging.DEBUG,
                        format=log_format,
                        filename=log_file)
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG)
    console.setFormatter(logging.Formatter(log_format))
    logging.getLogger(__name__).addHandler(console)
    return logging.getLogger(__name__)



def create_optimizer(model: nn.Cell):

    if cfg.TRAIN.OPTIMIZER == 'adam':
        optimizer = nn.Adam(model.trainable_params(),
                            learning_rate=cfg.TRAIN.LR,
                            weight_decay=cfg.TRAIN.WEIGHT_DECAY)
    elif cfg.TRAIN.OPTIMIZER == 'sgd':
        optimizer = nn.SGD(model.trainable_params(),
                           learning_rate=cfg.TRAIN.LR,
                           weight_decay=cfg.TRAIN.WEIGHT_DECAY,
                           momentum=cfg.TRAIN.MOMENTUM)
    elif cfg.TRAIN.OPTIMIZER == 'adam_onecycle':

        def children(m: nn.Cell):
            return list(m.cells())

        def num_children(m: nn.Cell) -> int:
            return len(children(m))

        flatten_model = lambda m: sum(map(flatten_model, m.cells()), []
                                      ) if num_children(m) else [m]
        get_layer_groups = lambda m: [nn.SequentialCell(flatten_model(m))]

        optimizer_func = partial(nn.Adam, betas=(0.9, 0.99))
        optimizer = OptimWrapper.create(optimizer_func,
                                        3e-3,
                                        get_layer_groups(model),
                                        wd=cfg.TRAIN.WEIGHT_DECAY,
                                        true_wd=True,
                                        bn_wd=True)

        # fix rpn: do this since we use costomized optimizer.step
        if cfg.RPN.ENABLED and cfg.RPN.FIXED:
            for param in model.rpn.get_parameters(expand=True):
                param.requires_grad = False
    else:
        raise NotImplementedError

    return optimizer


def create_scheduler(total_steps, batchs_in_epoch):

    def lr_lbmd(cur_epoch):
        cur_decay = 1
        for decay_step in cfg.TRAIN.DECAY_STEP_LIST:
            if cur_epoch >= decay_step:
                cur_decay = cur_decay * cfg.TRAIN.LR_DECAY
        return max(cur_decay, cfg.TRAIN.LR_CLIP / cfg.TRAIN.LR)

    def bnm_lmbd(cur_epoch):
        cur_decay = 1
        for decay_step in cfg.TRAIN.BN_DECAY_STEP_LIST:
            if cur_epoch >= decay_step:
                cur_decay = cur_decay * cfg.TRAIN.BN_DECAY
        return max(cfg.TRAIN.BN_MOMENTUM * cur_decay, cfg.TRAIN.BNM_CLIP)

    if cfg.TRAIN.OPTIMIZER == 'adam_onecycle':
        raise NotImplementedError
        lr_scheduler = lsf.OneCycle(optimizer, total_steps, cfg.TRAIN.LR,
                                    list(cfg.TRAIN.MOMS), cfg.TRAIN.DIV_FACTOR,
                                    cfg.TRAIN.PCT_START)
    else:
        # lr_scheduler = lr_sched.LambdaLR(optimizer, lr_lbmd, last_epoch=last_epoch)
        lr_scheduler = [
            lr_lbmd(cur_step // batchs_in_epoch)
            for cur_step in range(total_step)
        ]

    # bnm_scheduler = train_utils.BNMomentumScheduler(model,
    #                                                 bnm_lmbd,
    #                                                 last_epoch=last_epoch)
    # return lr_scheduler, bnm_scheduler
    return lr_scheduler





if __name__ == "__main__":
    if args.cfg_file is not None:
        cfg_from_file(args.cfg_file)
    cfg.TAG = os.path.splitext(os.path.basename(args.cfg_file))[0]

    if args.train_mode == 'rpn':
        cfg.RPN.ENABLED = True
        cfg.RCNN.ENABLED = False
        root_result_dir = os.path.join('../', 'output', 'rpn', cfg.TAG)
    elif args.train_mode == 'rcnn':
        cfg.RCNN.ENABLED = True
        cfg.RPN.ENABLED = cfg.RPN.FIXED = True
        root_result_dir = os.path.join('../', 'output', 'rcnn', cfg.TAG)
    elif args.train_mode == 'rcnn_offline':
        cfg.RCNN.ENABLED = True
        cfg.RPN.ENABLED = False
        root_result_dir = os.path.join('../', 'output', 'rcnn', cfg.TAG)
    else:
        raise NotImplementedError

    if args.output_dir is not None:
        root_result_dir = args.output_dir
    os.makedirs(root_result_dir, exist_ok=True)

    log_file = os.path.join(root_result_dir, 'log_train.txt')
    logger = create_logger(log_file)
    logger.info('**********************Start logging**********************')

    # log to file
    gpu_list = os.environ[
        'CUDA_VISIBLE_DEVICES'] if 'CUDA_VISIBLE_DEVICES' in os.environ.keys(
        ) else 'ALL'
    logger.info('CUDA_VISIBLE_DEVICES=%s' % gpu_list)

    for key, val in vars(args).items():
        logger.info("{:16} {}".format(key, val))

    save_config_to_file(cfg, logger=logger)

    # copy important files to backup
    backup_dir = os.path.join(root_result_dir, 'backup_files')
    os.makedirs(backup_dir, exist_ok=True)
    os.system('cp *.py %s/' % backup_dir)
    os.system('cp ../lib/net/*.py %s/' % backup_dir)
    os.system('cp ../lib/datasets/kitti_rcnn_dataset.py %s/' % backup_dir)

    # tensorboard log
    # tb_log = SummaryWriter(log_dir=os.path.join(root_result_dir, 'tensorboard'))

    # create dataloader & network & optimizer
    
    train_loader, test_loader,num_class = create_dataloader(logger,args=args)
    total_step = train_loader.get_dataset_size() * args.epochs

    net = PointRCNN(num_classes=num_class,
                    use_xyz=True,
                    mode='TEST')
    loss_net = net_with_loss(net,train_loader.get_col_names())
    
    # @TODO 学习率曲线未设置
    # lr_scheduler, bnm_scheduler = create_scheduler(optimizer, total_steps=total_step,
    #                                               batchs_in_epoch=train_loader.get_dataset_size())
    optimizer = create_optimizer(net)
    # optimizer.learning_rate = lr_scheduler
    # model = Model(loss_net,loss_fn=None,optimizer=optimizer)
    # load checkpoint if it is possible
    start_epoch = it = 0
    last_epoch = -1
    if args.ckpt is not None:
        load_checkpoint(args.ckpt, net)
        # it, start_epoch = train_utils.load_checkpoint(pure_model, optimizer, filename=args.ckpt, logger=logger)
        last_epoch = start_epoch + 1

    if args.rpn_ckpt is not None:
        raise NotImplementedError
        pure_model = model.module if isinstance(
            model, torch.nn.DataParallel) else model
        total_keys = pure_model.state_dict().keys().__len__()
        train_utils.load_part_ckpt(pure_model,
                                   filename=args.rpn_ckpt,
                                   logger=logger,
                                   total_keys=total_keys)

    if cfg.TRAIN.LR_WARMUP and cfg.TRAIN.OPTIMIZER != 'adam_onecycle':
        # should not enter
        pass
        # lr_warmup_scheduler = train_utils.CosineWarmupLR(
        #     optimizer,
        #     T_max=cfg.TRAIN.WARMUP_EPOCH * len(train_loader),
        #     eta_min=cfg.TRAIN.WARMUP_MIN)
    else:
        lr_warmup_scheduler = None

    # start training
    logger.info('**********************Start training**********************')
    ckpt_dir = os.path.join(root_result_dir, 'ckpt')
    os.makedirs(ckpt_dir, exist_ok=True)

    cb = [LossMonitor(200),TimeMonitor(200)]
    # trainer = train_utils.Trainer(
    #     model,
    #     train_functions.model_joint_fn_decorator(),
    #     optimizer,
    #     ckpt_dir=ckpt_dir,
    #     lr_scheduler=lr_scheduler,
    #     bnm_scheduler=bnm_scheduler,
    #     model_fn_eval=train_functions.model_joint_fn_decorator(),
    #     tb_log=tb_log,
    #     eval_frequency=1,
    #     lr_warmup_scheduler=lr_warmup_scheduler,
    #     warmup_epoch=cfg.TRAIN.WARMUP_EPOCH,
    #     grad_norm_clip=cfg.TRAIN.GRAD_NORM_CLIP)

    # trainer.train(
    #     it,
    #     start_epoch,
    #     args.epochs,
    #     train_loader,
    #     test_loader,
    #     ckpt_save_interval=args.ckpt_save_interval,
    #     lr_scheduler_each_iter=(cfg.TRAIN.OPTIMIZER == 'adam_onecycle'))

    # model.train(args.epochs,train_loader,cb,dataset_sink_mode=False)
    logger.info('**********************End training**********************')
    # for name, param in net.parameters_and_names():
    #     print(name, param.shape)
    params = load_checkpoint('../PointRCNN.ckpt')
    load_param_into_net(net, params)
    net.set_train(False)

    for name, param in net.parameters_and_names():
        print(name, param.mean(), param.max())
        break

    np.random.seed(66)
    inputs = np.random.randn(1, 16384, 3)
    inputs = Tensor(inputs, dtype=ms.float32)
    print('inputs: ', inputs.mean())
    ret_dict = net({'pts_input': inputs})
    
    roi_scores_raw = ret_dict['roi_scores_raw']  # (B, M)
    roi_boxes3d = ret_dict['rois']  # (B, M, 7)
    seg_result = ret_dict['seg_result']

    print(roi_scores_raw.mean())
    print(roi_boxes3d.mean())
