#! /usr/bin/env python
'''

This script is for making directories for the test_real and test_synthetic data folder for testing of rotationnet in icra2020.
Author: Hongtao Wu
Contact: hwu67@jhu.edu

Aug 19, 2019

'''
import shutil
import os

target_dir = '/home/hwu67/pytorch-rotationnet/ModelNet40_20_chair/test_real_upright'
source_dir = '/home/hwu67/pytorch-rotationnet/ModelNet40_20_chair/train'

model_type_list= os.listdir(source_dir)

for model_type in model_type_list:
	os.makedirs(os.path.join(target_dir, model_type), exist_ok=True)
	print('Model {} folder is made!'.format(model_type))
