from __future__ import print_function

import os
import sys
import time
import torch
import torch.optim as optim
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.distributed as dist
import argparse
import socket
from torch.utils.data import distributed
import tensorboard_logger as tb_logger

from torchvision import transforms, datasets
from dataset import RGB2Lab, RGB2YCbCr
from util import adjust_learning_rate, AverageMeter, accuracy

from models.alexnet import MyAlexNetCMC, MyAlexNetCMC_cc
from models.resnet_beta import MyResNetsCMC
### change this to test ll, knn and liblinear
from models.LinearModel_beta import LinearClassifierAlexNet, LinearClassifierResNet_L, LinearClassifierResNet_Lab
###
import numpy as np
#####
from corruption import create_augmentation
#####
# from spawn import spawn

def parse_option():

	parser = argparse.ArgumentParser('argument for training')

	parser.add_argument('--print_freq', type=int, default=10, help='print frequency')
	parser.add_argument('--tb_freq', type=int, default=500, help='tb frequency')
	parser.add_argument('--save_freq', type=int, default=5, help='save frequency')
	parser.add_argument('--batch_size', type=int, default=256, help='batch_size')
	parser.add_argument('--num_workers', type=int, default=32, help='num of workers to use')
	parser.add_argument('--epochs', type=int, default=60, help='number of training epochs')

	# optimization
	parser.add_argument('--learning_rate', type=float, default=0.1, help='learning rate')
	parser.add_argument('--lr_decay_epochs', type=str, default='30,40,50', help='where to decay lr, can be a list')
	parser.add_argument('--lr_decay_rate', type=float, default=0.2, help='decay rate for learning rate')
	parser.add_argument('--momentum', type=float, default=0.9, help='momentum')
	parser.add_argument('--weight_decay', type=float, default=0, help='weight decay')
	parser.add_argument('--beta1', type=float, default=0.5, help='beta1 for Adam')
	parser.add_argument('--beta2', type=float, default=0.999, help='beta2 for Adam')

	parser.add_argument('--resume', default='', type=str, metavar='PATH',
						help='path to latest checkpoint (default: none)')

	# model definition
	parser.add_argument('--model', type=str, default='alexnet')
	parser.add_argument('--model_path', type=str, default=None, help='the model to test')
	parser.add_argument('--layer', type=int, default=6, help='which layer to evaluate')

	# dataset
	parser.add_argument('--dataset', type=str, default='imagenet', choices=['imagenet100', 'imagenet', 'cifar'])

	# add new views
	parser.add_argument('--view', type=str, default='Lab')
	parser.add_argument('--oracle', type=str, default='original')
	parser.add_argument('--feat_version', type=str, default='Lab')
	# parser.add_argument('--corruption', type=str, default='original')
	parser.add_argument('--level', type=int, default=5, help='The level of corruption')
	# path definition
	parser.add_argument('--data_folder', type=str, default=None, help='path to data')
	parser.add_argument('--save_path', type=str, default=None, help='path to save linear classifier')
	parser.add_argument('--tb_path', type=str, default=None, help='path to tensorboard')

	# data crop threshold
	parser.add_argument('--crop_low', type=float, default=0.2, help='low area in crop')

	# log file
	parser.add_argument('--log', type=str, default='time_linear.txt', help='log file')

	# GPU setting
	parser.add_argument('--gpu', default=None, type=int, help='GPU id to use.')

	opt = parser.parse_args()

	##
	opt.model_path = opt.model_path.replace('augment', opt.view)
	##

	if (opt.data_folder is None) or (opt.save_path is None) or (opt.tb_path is None):
		raise ValueError('one or more of the folders is None: data_folder | save_path | tb_path')

	if opt.dataset == 'imagenet':
		if 'alexnet' not in opt.model:
			opt.crop_low = 0.08

	iterations = opt.lr_decay_epochs.split(',')
	opt.lr_decay_epochs = list([])
	for it in iterations:
		opt.lr_decay_epochs.append(int(it))

	opt.model_name = opt.model_path.split('/')[-2]
	# opt.model_name = 'calibrated_{}_bsz_{}_lr_{}_decay_{}'.format(opt.model_name, opt.batch_size, opt.learning_rate,
	# 															  opt.weight_decay)
	opt.model_name = 'calibrated_{}_bsz_{}_lr_{}_decay_{}_{}'.format(opt.model_name, opt.batch_size, opt.learning_rate,
																opt.weight_decay, opt.oracle)

	# opt.model_name = '{}_view_{}'.format(opt.model_name, opt.view)
	# opt.model_name = '{}_view_{}'.format(opt.model_name, opt.corruption)
	# Corruption is not useful when training linear classifier

	opt.tb_folder = os.path.join(opt.tb_path, opt.model_name + '_layer{}'.format(opt.layer))
	if not os.path.isdir(opt.tb_folder):
		os.makedirs(opt.tb_folder)

	opt.save_folder = os.path.join(opt.save_path, opt.model_name)
	if not os.path.isdir(opt.save_folder):
		os.makedirs(opt.save_folder)

	if opt.dataset == 'imagenet100':
		opt.n_label = 100
	if opt.dataset == 'imagenet':
		opt.n_label = 1000
	if opt.dataset == 'cifar':
		opt.n_label = 10

	return opt

def get_train_val_loader(args):
	if args.view == 'Lab' or args.view == 'YCbCr':
		if args.view == 'Lab':
			mean = [(0 + 100) / 2, (-86.183 + 98.233) / 2, (-107.857 + 94.478) / 2]
			std = [(100 - 0) / 2, (86.183 + 98.233) / 2, (107.857 + 94.478) / 2]
			color_transfer = RGB2Lab()
		else:
			mean = [116.151, 121.080, 132.342]
			std = [109.500, 111.855, 111.964]
			color_transfer = RGB2YCbCr()
		normalize = transforms.Normalize(mean=mean, std=std)
		train_dataset = datasets.CIFAR10(
			root=args.data_folder,
			train=True,
			transform=transforms.Compose([
				# transforms.RandomResizedCrop(224, scale=(args.crop_low, 1.0)),
				transforms.RandomCrop(32, padding=4), # maybe not necessary
				transforms.RandomHorizontalFlip(),
				color_transfer,
				transforms.ToTensor(),
				normalize,
			])
		)
		val_dataset = datasets.CIFAR10(
			root=args.data_folder,
			train=False,
			transform=transforms.Compose([
				color_transfer,
				transforms.ToTensor(),
				normalize,
			])
		)
		if args.oracle != 'original':
			if args.oracle != 'scale':
				train_raw = np.load(args.data_folder + '/CIFAR-10-C-trainval/train/%s_4_images.npy' %(args.oracle))
				train_dataset.data = train_raw
				val_raw = np.load(args.data_folder + '/CIFAR-10-C-trainval/val/%s_4_images.npy' %(args.oracle))
				val_dataset.data = val_raw
			else:
				train_raw = np.load(args.data_folder + '/CIFAR-10-C-trainval/train/upsample_4_images.npy')
				train_dataset.data = train_raw
				val_raw = np.load(args.data_folder + '/CIFAR-10-C-trainval/val/upsample_4_images.npy')
				val_dataset.data = val_raw
	else:
		print('Use RGB images with %s level %s!' %(args.view, str(args.level)))
		NORM = ((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
		normalize_lst = lambda x: list(map(transforms.Normalize(*NORM), x))
		data_augmentation = create_augmentation(args.view, args.level)
		train_transform = transforms.Compose([
			transforms.RandomCrop(32, padding=4), # maybe not necessary
			# transforms.Resize(224),
			transforms.RandomHorizontalFlip(),
			transforms.ToTensor(),
			data_augmentation,
			normalize_lst
		])
		train_dataset = datasets.CIFAR10(root=args.data_folder,
			train=True, download=True, transform=train_transform)

		val_transform = transforms.Compose([
			transforms.ToTensor(),
			data_augmentation,
			normalize_lst
		])
		val_dataset = datasets.CIFAR10(root=args.data_folder,
			train=False, download=True, transform=val_transform)

	print('number of train: {}'.format(len(train_dataset)))
	print('number of val: {}'.format(len(val_dataset)))

	train_sampler = None

	train_loader = torch.utils.data.DataLoader(
		train_dataset, batch_size=args.batch_size, shuffle=(train_sampler is None),
		num_workers=args.num_workers, pin_memory=True, sampler=train_sampler)

	val_loader = torch.utils.data.DataLoader(
		val_dataset, batch_size=args.batch_size, shuffle=False,
		num_workers=args.num_workers, pin_memory=True)

	return train_loader, val_loader, train_sampler

def set_model(args):
	if args.model.startswith('alexnet'):
		model = MyAlexNetCMC()
		classifier = LinearClassifierAlexNet(layer=args.layer, n_label=args.n_label, pool_type='max')
	elif args.model.startswith('resnet'):
		model = MyResNetsCMC(name=args.model, view=args.view, level=args.level)
		if args.model.endswith('v1'):
			classifier = LinearClassifierResNet_Lab(args.layer, args.n_label, 'avg', 1)
		elif args.model.endswith('v2'):
			classifier = LinearClassifierResNet_Lab(args.layer, args.n_label, 'avg', 2)
		elif args.model.endswith('v3'):
			classifier = LinearClassifierResNet_Lab(args.layer, args.n_label, 'avg', 4)
		elif 'ttt' in args.model:
			if args.feat_version == 'L':
				classifier = LinearClassifierResNet_L(10, args.n_label, 'avg', 1)
			else:
				classifier = LinearClassifierResNet_Lab(10, args.n_label, 'avg', 1)
		else:
			raise NotImplementedError('model not supported {}'.format(args.model))
	else:
		raise NotImplementedError('model not supported {}'.format(args.model))

	# load pre-trained model
	print('==> loading pre-trained model')
	ckpt = torch.load(args.model_path)
	model.load_state_dict(ckpt['model'])
	print("==> loaded checkpoint '{}' (epoch {})".format(args.model_path, ckpt['epoch']))
	print('==> done')

	model = model.cuda()
	classifier = classifier.cuda()

	model.eval()

	criterion = nn.CrossEntropyLoss().cuda(args.gpu)

	return model, classifier, criterion

def set_optimizer(args, classifier):
	optimizer = optim.SGD(classifier.parameters(),
						  lr=args.learning_rate,
						  momentum=args.momentum,
						  weight_decay=args.weight_decay)
	return optimizer

def train(epoch, train_loader, model, classifier, criterion, optimizer, opt):
	"""
	one epoch training
	"""
	model.eval()
	classifier.train()

	batch_time = AverageMeter()
	data_time = AverageMeter()
	losses = AverageMeter()
	top1 = AverageMeter()
	top5 = AverageMeter()

	end = time.time()
	for idx, (input, target) in enumerate(train_loader):
		# measure data loading time
		data_time.update(time.time() - end)

		# input = input.float()
		if opt.gpu is not None:
			# input = input.cuda(opt.gpu, non_blocking=True)
			input = list(map( lambda x: x.float().cuda(opt.gpu, non_blocking=True), input ))
		target = target.cuda(opt.gpu, non_blocking=True)

		# ===================forward=====================
		with torch.no_grad():
			feat_l, feat_ab = model(input, opt.layer)
			if opt.feat_version == 'L':
				feat = feat_l.detach()
			elif opt.feat_version == 'LL':
				feat = torch.cat((feat_l.detach(), feat_l.detach()), dim=1)
			else:
				feat = torch.cat((feat_l.detach(), feat_ab.detach()), dim=1)

		output = classifier(feat)
		##### This may be not neccessary
		target = target.long()
		#####
		loss = criterion(output, target)

		acc1, acc5 = accuracy(output, target, topk=(1, 5))
		# losses.update(loss.item(), input.size(0))
		# top1.update(acc1[0], input.size(0))
		# top5.update(acc5[0], input.size(0))
		losses.update(loss.item(), input[0].size(0))
		top1.update(acc1[0], input[0].size(0))
		top5.update(acc5[0], input[0].size(0))

		# ===================backward=====================
		optimizer.zero_grad()
		loss.backward()
		optimizer.step()

		# ===================meters=====================
		batch_time.update(time.time() - end)
		end = time.time()

		# print info
		if idx % opt.print_freq == 0:
			print('Epoch: [{0}][{1}/{2}]\t'
				  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
				  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
				  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
				  'Acc@1 {top1.val:.3f} ({top1.avg:.3f})\t'
				  'Acc@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
				   epoch, idx, len(train_loader), batch_time=batch_time,
				   data_time=data_time, loss=losses, top1=top1, top5=top5))
			sys.stdout.flush()

	return top1.avg, top5.avg, losses.avg

def validate(val_loader, model, classifier, criterion, opt):
	"""
	evaluation
	"""
	batch_time = AverageMeter()
	losses = AverageMeter()
	top1 = AverageMeter()
	top5 = AverageMeter()

	# switch to evaluate mode
	model.eval()
	classifier.eval()

	with torch.no_grad():
		end = time.time()
		for idx, (input, target) in enumerate(val_loader):
			# input = input.float()
			if opt.gpu is not None:
				# input = input.cuda(opt.gpu, non_blocking=True)
				input = list(map( lambda x: x.float().cuda(opt.gpu, non_blocking=True), input ))
			target = target.cuda(opt.gpu, non_blocking=True)

			# compute output
			feat_l, feat_ab = model(input, opt.layer)
			if opt.feat_version == 'L':
				feat = feat_l.detach()
			elif opt.feat_version == 'LL':
				feat = torch.cat((feat_l.detach(), feat_l.detach()), dim=1)
			else:
				feat = torch.cat((feat_l.detach(), feat_ab.detach()), dim=1)
			
			output = classifier(feat)
			##### This may be not necessary
			target = target.long()
			#####
			loss = criterion(output, target)

			# measure accuracy and record loss
			acc1, acc5 = accuracy(output, target, topk=(1, 5))
			# losses.update(loss.item(), input.size(0))
			# top1.update(acc1[0], input.size(0))
			# top5.update(acc5[0], input.size(0))
			losses.update(loss.item(), input[0].size(0))
			top1.update(acc1[0], input[0].size(0))
			top5.update(acc5[0], input[0].size(0))

			# measure elapsed time
			batch_time.update(time.time() - end)
			end = time.time()

			if idx % opt.print_freq == 0:
				print('Test: [{0}/{1}]\t'
					  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
					  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
					  'Acc@1 {top1.val:.3f} ({top1.avg:.3f})\t'
					  'Acc@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
					   idx, len(val_loader), batch_time=batch_time, loss=losses,
					   top1=top1, top5=top5))

		print(' * Acc@1 {top1.avg:.3f} Acc@5 {top5.avg:.3f}'
			  .format(top1=top1, top5=top5))

	return top1.avg, top5.avg, losses.avg

def main():
	global best_acc1
	best_acc1 = 0

	args = parse_option()

	if args.gpu is not None:
		print("Use GPU: {} for training".format(args.gpu))

	# set the data loader
	train_loader, val_loader, train_sampler = get_train_val_loader(args)
	# set the model
	model, classifier, criterion = set_model(args)

	# set optimizer
	optimizer = set_optimizer(args, classifier)

	cudnn.benchmark = True

	# optionally resume linear classifier
	args.start_epoch = 1
	if args.resume:
		if os.path.isfile(args.resume):
			print("=> loading checkpoint '{}'".format(args.resume))
			checkpoint = torch.load(args.resume)
			args.start_epoch = checkpoint['epoch'] + 1
			best_acc1 = checkpoint['best_acc1']
			if args.gpu is not None:
				# best_acc1 may be from a checkpoint from a different GPU
				best_acc1 = best_acc1.to(args.gpu)
			classifier.load_state_dict(checkpoint['classifier'])
			optimizer.load_state_dict(checkpoint['optimizer'])
			print("=> loaded checkpoint '{}' (epoch {})"
				  .format(args.resume, checkpoint['epoch']))
		else:
			print("=> no checkpoint found at '{}'".format(args.resume))

	args.start_epoch = 1
	if args.resume:
		if os.path.isfile(args.resume):
			print("=> loading checkpoint '{}'".format(args.resume))
			checkpoint = torch.load(args.resume, map_location='cpu')
			args.start_epoch = checkpoint['epoch'] + 1
			classifier.load_state_dict(checkpoint['classifier'])
			optimizer.load_state_dict(checkpoint['optimizer'])
			best_acc1 = checkpoint['best_acc1']
			best_acc1 = best_acc1.cuda()
			print("=> loaded checkpoint '{}' (epoch {})"
				  .format(args.resume, checkpoint['epoch']))
			del checkpoint
			torch.cuda.empty_cache()
		else:
			print("=> no checkpoint found at '{}'".format(args.resume))

	# tensorboard
	logger = tb_logger.Logger(logdir=args.tb_folder, flush_secs=2)

	# routine
	for epoch in range(args.start_epoch, args.epochs + 1):

		adjust_learning_rate(epoch, args, optimizer)
		print("==> training...")

		time1 = time.time()
		train_acc, train_acc5, train_loss = train(epoch, train_loader, model, classifier, criterion, optimizer, args)
		time2 = time.time()
		print('train epoch {}, total time {:.2f}'.format(epoch, time2 - time1))

		logger.log_value('train_acc', train_acc, epoch)
		logger.log_value('train_acc5', train_acc5, epoch)
		logger.log_value('train_loss', train_loss, epoch)

		print("==> testing...")
		test_acc, test_acc5, test_loss = validate(val_loader, model, classifier, criterion, args)

		logger.log_value('test_acc', test_acc, epoch)
		logger.log_value('test_acc5', test_acc5, epoch)
		logger.log_value('test_loss', test_loss, epoch)

		# save the best model
		if test_acc > best_acc1:
			best_acc1 = test_acc
			state = {
				'opt': args,
				'epoch': epoch,
				'classifier': classifier.state_dict(),
				'best_acc1': best_acc1,
				'optimizer': optimizer.state_dict(),
			}
			save_name = '{}_layer{}.pth'.format(args.model, args.layer)
			save_name = os.path.join(args.save_folder, save_name)
			print('saving best model!')
			torch.save(state, save_name)

		# save model
		if epoch % args.save_freq == 0:
			print('==> Saving...')
			state = {
				'opt': args,
				'epoch': epoch,
				'classifier': classifier.state_dict(),
				'best_acc1': test_acc,
				'optimizer': optimizer.state_dict(),
			}
			save_name = 'ckpt_epoch_{epoch}.pth'.format(epoch=epoch)
			save_name = os.path.join(args.save_folder, save_name)
			print('saving regular model!')
			torch.save(state, save_name)

		# tensorboard logger
		pass

if __name__ == '__main__':
	best_acc1 = 0
	main()