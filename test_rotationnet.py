'''

This code is written for computing the accuracy of the prediction of the rotationnet (ICRA2020).
Author: Hongtao Wu
Contact: hwu67@jhu.edu

Aug 19, 2019

'''
from __future__ import division

import argparse
import os
import shutil
import time
import csv

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.nn.functional
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import torchvision.models as models
import numpy as np

os.environ["CUDA_VISIBLE_DEVICES"]= '1'
model_names = sorted(name for name in models.__dict__
    if name.islower() and not name.startswith("__")
    and callable(models.__dict__[name]))

parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
parser.add_argument('-d', '--data', default='./ModelNet40_20', type=str, 
                    help='path to dataset')
parser.add_argument('--arch', '-a', metavar='ARCH', default='resnet18',
                    choices=model_names,
                    help='model architecture: ' +
                        ' | '.join(model_names) +
                        ' (default: resnet18)')
parser.add_argument('-b', '--batch-size', default=20, type=int,
                    metavar='N', help='mini-batch size (default: 20)')
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N', help='number of data loading workers (default: 4)')
parser.add_argument('--lr', '--learning_rate', default=0.1, type=float, metavar='LR', help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M', help='momentum')
parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)')
parser.add_argument('--print-freq', '-p', default=10, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('-r', '--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('--pretrained', dest='pretrained', action='store_true',
                    help='use pre-trained model')
parser.add_argument('--world-size', default=1, type=int,
                    help='number of distributed processes')
parser.add_argument('--dist-url', default='tcp://224.66.41.62:23456', type=str,
                    help='url used to set up distributed training')
parser.add_argument('--dist-backend', default='gloo', type=str,
                    help='distributed backend')
parser.add_argument('--case', default='2', type=str,
                    help='viewpoint setup case (1 or 2)')
parser.add_argument('--type', type=str, help='type of data being tested: test_real, test_synthetic, test')
parser.add_argument('--csvpath', type=str, help='csv path for saving the chair score')

best_prec1 = 0
vcand = np.load('vcand_case2.npy')
nview = 20

class FineTuneModel(nn.Module):
    def __init__(self, original_model, arch, num_classes):
        super(FineTuneModel, self).__init__()

        if arch.startswith('alexnet') :
            self.features = original_model.features
            self.classifier = nn.Sequential(
                nn.Dropout(),
                nn.Linear(256 * 6 * 6, 4096),
                nn.ReLU(inplace=True),
                nn.Dropout(),
                nn.Linear(4096, 4096),
                nn.ReLU(inplace=True),
                nn.Linear(4096, num_classes),
            )
            self.modelName = 'alexnet'
        elif arch.startswith('resnet') :
            # Everything except the last linear layer
            self.features = nn.Sequential(*list(original_model.children())[:-1])
            self.classifier = nn.Sequential(
                nn.Linear(512, num_classes)
            )
            self.modelName = 'resnet'
        elif arch.startswith('vgg16'):
            self.features = original_model.features
            self.classifier = nn.Sequential(
                nn.Dropout(),
                nn.Linear(25088, 4096),
                nn.ReLU(inplace=True),
                nn.Dropout(),
                nn.Linear(4096, 4096),
                nn.ReLU(inplace=True),
                nn.Linear(4096, num_classes),
            )
            self.modelName = 'vgg16'
        else :
            raise("Finetuning not supported on this architecture yet")

        # # Freeze those weights
        # for p in self.features.parameters():
        #     p.requires_grad = False


    def forward(self, x):
        f = self.features(x)
        if self.modelName == 'alexnet' :
            f = f.view(f.size(0), 256 * 6 * 6)
        elif self.modelName == 'vgg16':
            f = f.view(f.size(0), -1)
        elif self.modelName == 'resnet' :
            f = f.view(f.size(0), -1)
        y = self.classifier(f)
        return y


# Child Class to retrieve the original file path
class RotationNetDataset(datasets.folder.ImageFolder):
    def __init__(self, root, transform=None, target_transform=None):
	super(RotationNetDataset, self).__init__(root, transform=transform, target_transform=target_transform)

    def __getitem__(self, index):
	path, target = self.samples[index]
      	sample = self.loader(path)
	if self.transform is not None:
	    sample = self.transform(sample)
  	if self.target_transform is not None:
	    target = self.target_transform(target)

	return sample, target, path


def main():
    global args, best_prec1, nview, vcand
    args = parser.parse_args()

    args.distributed = args.world_size > 1

    if args.case == '1':
        vcand = np.load('vcand_case1.npy')
        nview = 12
    elif args.case == '3':
        vcand = np.load('vcand_case3.npy')
        nview = 160

    if args.batch_size % nview != 0:
        print 'Error: batch size should be multiplication of the number of views,', nview
        exit()

    if args.distributed:
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size)

    traindir = os.path.join(args.data, 'train')
    # Get number of classes from train directory
    num_classes = len([name for name in os.listdir(traindir)])
    print("num_classes = '{}'".format(num_classes))

    # create model
    if args.pretrained:
        print("=> using pre-trained model '{}'".format(args.arch))
        model = models.__dict__[args.arch](pretrained=True)
    else:
        print("=> creating model '{}'".format(args.arch))
        model = models.__dict__[args.arch]()

    model = FineTuneModel(model, args.arch, (num_classes+1) * nview )

    if not args.distributed:
        if args.arch.startswith('alexnet') or args.arch.startswith('vgg'):
            model.features = torch.nn.DataParallel(model.features)
            model.cuda()
        else:
            model = torch.nn.DataParallel(model).cuda()
    else:
        model.cuda()
        model = torch.nn.parallel.DistributedDataParallel(model)

    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda()

    optimizer = torch.optim.SGD(filter(lambda p: p.requires_grad, model.parameters()),
				args.lr,
				momentum=args.momentum,
				weight_decay=args.weight_decay)

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            best_prec1 = checkpoint['best_prec1']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    cudnn.benchmark = True

    # Data loading code
    testdir = os.path.join(args.data, args.type) 
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])


    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
    else:
        train_sampler = None

#    test_loader = torch.utils.data.DataLoader(
#        datasets.ImageFolder(testdir, transforms.Compose([
##            transforms.Scale(256),
##            transforms.CenterCrop(224),
#            transforms.ToTensor(),
#            normalize,
#        ])),
#        batch_size=args.batch_size, shuffle=False,
#        num_workers=args.workers, pin_memory=True)
    test_loader = torch.utils.data.DataLoader(
	 RotationNetDataset(testdir, transform=transforms.Compose([
	     transforms.ToTensor(),
             normalize,
	 ])),
	 batch_size=args.batch_size, shuffle=False,
	 num_workers=args.workers, pin_memory=True)

    test_loader.dataset.imgs = sorted(test_loader.dataset.imgs)

    validate(test_loader, model, criterion, args.csvpath)


def validate(test_loader, model, criterion, csv_path):
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to evaluate mode
    model.eval()

    # writerow to save the score of the chair for PR curve
    writerow = {} 

    end = time.time()

    # chair vs nonchair classification result calculation
    gt_is_chair_num = 0
    gt_is_nonchair_num = 0
    classification_chair_correct = 0
    classification_nonchair_correct = 0

    for i, (input, target, path) in enumerate(test_loader):
	
	# Debug
	print("########## Debug ############")
	model_name = path[0].split('/')[-1].split('.')[0][:-4]
	print model_name
#	print('Target: {}'.format(target))
#	print("Test Loader: {}".format(test_loader[i]))

        target = target.cuda(async=True)
        input_var = torch.autograd.Variable(input, volatile=True)
        target_var = torch.autograd.Variable(target, volatile=True)

        # compute output
        output = model(input_var)
        loss = criterion(output, target_var)
	
#	print 'Raw Output Shape: ', output.shape
	
        # log_softmax and reshape output
        num_classes = int( output.size( 1 ) / nview ) - 1
        output = output.view( -1, num_classes + 1 )
        output = torch.nn.functional.log_softmax( output )
        output = output[ :, :-1 ] - torch.t( output[ :, -1 ].repeat( 1, output.size(1)-1 ).view( output.size(1)-1, -1 ) )
        output = output.view( -1, nview * nview, num_classes )
		
#	print 'Softmax Output: ', output	
#	print 'Softmax Output Shape: ', output.shape
        # measure accuracy and record loss
        res, classification_result, classification_gt_is_chair, model_chair_score = my_accuracy(output.data, target, model_name, topk=(1, 5))
        prec1, prec5 = res
	
	# writerow update new model	
	writerow[model_name] = model_chair_score.cpu().data.numpy().astype(np.float64).item()

        if classification_gt_is_chair == 1: 
	    gt_is_chair_num += 1
            classification_chair_correct += classification_result
        elif classification_gt_is_chair == 0:
            gt_is_nonchair_num += 1
            classification_nonchair_correct += classification_result
	else:
            raise ValueError("The ground truth has to be chair or nonchair!!!!")

        losses.update(loss.data[0], input.size(0))
        top1.update(prec1[0], input.size(0)/nview)
        top5.update(prec5[0], input.size(0)/nview)

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            print('Test: [{0}/{1}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                  'Prec@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                   i, len(test_loader), batch_time=batch_time, loss=losses,
                   top1=top1, top5=top5))

    print(' * Prec@1 {top1.avg:.3f} Prec@5 {top5.avg:.3f}'
          .format(top1=top1, top5=top5))
    
    if args.type != 'test_real':
        chair_classification_nonchair_accuracy = classification_nonchair_correct / gt_is_nonchair_num
        print("Non chair classification acc.: {}".format(chair_classification_nonchair_accuracy))

    chair_classification_overall_accuracy = (classification_chair_correct + classification_nonchair_correct) / (gt_is_chair_num + gt_is_nonchair_num)
    print("Classification Chair Correct: {}".format(classification_chair_correct))
    print("Groundtruch Chair Num: {}".format(gt_is_chair_num))
    classification_chair_accuracy = classification_chair_correct / gt_is_chair_num
    print "Chair classification acc.: %.5f" % (classification_chair_correct / gt_is_chair_num)
    print "Overall classifocatopm acc.: %.5f" % (chair_classification_overall_accuracy)    

    # Write CSV
    with open(csv_path, 'w') as csv_file:
	writer = csv.writer(csv_file)
	for model_name in writerow.keys():
	    row = []
	    row.append(model_name)
	    row.append(writerow[model_name])
	    writer.writerow(row)
#    print("Chair Score: ", writerow)

    return top1.avg


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        #correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res

def my_accuracy(output_, target, model_name, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    target = target[0:-1:nview]
    batch_size = target.size(0)

    num_classes = output_.size(2)
    output_ = output_.cpu().numpy()
    output_ = output_.transpose( 1, 2, 0 )
    scores = np.zeros( ( vcand.shape[ 0 ], num_classes, batch_size ) )
    output = torch.zeros( ( batch_size, num_classes ) )
    # compute scores for all the candidate poses (see Eq.(6))
    for j in range(vcand.shape[0]):
        for k in range(vcand.shape[1]):
            scores[ j ] = scores[ j ] + output_[ vcand[ j ][ k ] * nview + k ]
	
    ##### Debug #####
#    print 'Scores: ', scores
#    print 'Scores shape: ', scores.shape
    #################

    # for each sample #n, determine the best pose that maximizes the score (for the top class)
    for n in range( batch_size ):
        j_max = int( np.argmax( scores[ :, :, n ] ) / scores.shape[ 1 ] )
        output[ n ] = torch.FloatTensor( scores[ j_max, :, n ] )
    output = output.cuda()

#    print 'output: ', output
    chair_score = output[0][8]
    print 'chair score: ', chair_score

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    
    print 'prediction: ', pred
    target_class = target.cpu().data.numpy()[0]
    print("Target Class: {}".format(target_class))
    prediction_class = pred.cpu().data.numpy()[0][0]
    print ("Prediction Class: {}".format(prediction_class))
    
    # Classification results true/false
    classification_correct = 0
    # Example is chair/non-chair 
    classification_gt_is_chair = 0
    
    # Example is a chair
    if target_class == 8 and model_name != 'chair_0944' and model_name != 'chair_0950':
	classification_gt_is_chair = 1
	if prediction_class == 8:
            classification_correct = 1
    # Example is a non-chair
    elif target_class != 8 or model_name == 'chair_0944' or model_name == 'chair_0950':
        if prediction_class != 8:
            classification_correct = 1		
    
    correct = pred.eq(target.contiguous().view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res, classification_correct, classification_gt_is_chair, chair_score


if __name__ == '__main__':
    main()
