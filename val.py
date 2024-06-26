import argparse
import os
import torch
import json
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from tqdm import tqdm
from torch.utils.data.dataloader import DataLoader

from models.hrnet.hrnet import HRNet
from utils.loss import JointsMSELoss
from utils.datasets import PoseDataset
from misc.utils import get_max_preds
from utils.metrics import evaluate_pck_accuracy
from misc.results_postprocessing import get_results_summary

# !!!!
# Note: 
# parameters passed to the model are curretly hardcoded (need to add as argument)





def _calc_dists(preds, target, normalize):
    preds = preds.type(torch.float32)     # pred joint coords
    target = target.type(torch.float32)   # target joint coords
    dists = torch.zeros((preds.shape[1], preds.shape[0])).to(preds.device)
    for n in range(preds.shape[0]):
        for c in range(preds.shape[1]):
            if target[n, c, 0] > 1 and target[n, c, 1] > 1:
                normed_preds = preds[n, c, :] / normalize[n]
                normed_targets = target[n, c, :] / normalize[n]
                # # dists[c, n] = np.linalg.norm(normed_preds - normed_targets)
                dists[c, n] = torch.norm(normed_preds - normed_targets)
            else:
                dists[c, n] = -1
    return dists


def run(dataset,
        weights,
        batch_size,
        device,
        pck_thr,
        vis_enabled,
        num_workers=1):

    # set device and load model
    if device is None:
        if torch.cuda.is_available():
            device = torch.device('cuda:0')
        else:
            device = torch.device('cpu')
    print('device: ', device)

    model = HRNet(c=48, nof_joints=8, bn_momentum=0.1).to(device)
    model.eval()

    # define loss
    loss_fn = JointsMSELoss().to(device)

    # load checkpoint
    print("Loading checkpoint ...\n", weights, '\n')
    checkpoint = torch.load(weights, map_location=device)
    epoch = checkpoint['epoch']
    print("Checkpoint's epoch: ", epoch)
    model.load_state_dict(checkpoint['model'])
    
    # load dataset and dataloader
    ds = PoseDataset(dataset_dir=dataset, is_train=False, vis_enabled=vis_enabled)
    #image, heatmaps_gt, target_weight, sample_data = ds.__getitem__(19)
    #print(target_weight)
    dataloader = DataLoader(ds, batch_size=batch_size, shuffle=False, num_workers = num_workers)
    print('batch_size: ', batch_size)
    print('dataset length : ', len(dataloader)*batch_size)
    
   
    # initialize variables 
    loss_all = []
    acc_all = []
    NE_all = []
    preds_all = []
    results = []
    with torch.no_grad():
        pbar = tqdm(dataloader, desc='Evaluation')
        print(pbar)
        for step, (image, target, target_weight, joints_data) in enumerate(pbar):
                
            image = image.to(device)
            target = target.to(device)
            target_weight = target_weight.to(device)
            #print(target_weight)
            
            ###===============#### do the prediction ####===============#####==========================###
            output = model(image)            
            
            ### Min kode her ###-------------------------------------------------------------------------------------
            # Zero out heatmaps for non-visible keypoints
            for i in range(output.shape[0]):  # Loop through the batch
                for j in range(output.shape[1]):  # Loop through each keypoint heatmap
                    if target_weight[i, j] != 1:  # Check if the keypoint is not visible
                        output[i, j, :, :] = 0  # Set the heatmap to zeros
            #---------------------------------------------------------------------------------------------------------

            # calculate loss
            loss = loss_fn(output, target, target_weight)

            # calculation accuracy (pck)
            accs, avg_acc, cnt, joints_preds, joints_targets, NEs = evaluate_pck_accuracy(output, target, thr=pck_thr)
            
            # prepare preds for saving in csv
            joints_preds = joints_preds.squeeze().cpu().numpy()
            joints_targets = joints_targets.squeeze().cpu().numpy()
            target_weight = target_weight.squeeze().cpu().numpy()
            
            preds = []
            preds.append(joints_data['imgId'][0])
            for i in range(8):
                preds.append(joints_targets[i][0])
                preds.append(joints_targets[i][1])
                

            for i in range(8):
                preds.append(joints_preds[i][0])
                preds.append(joints_preds[i][1])

            for i in range (8):
                preds.append(target_weight[i])
                
                                
            valid_NEs = [ne for ne in NEs if ne != -1]
            sampleNE = sum(valid_NEs)/len(valid_NEs)
            sampleNE = sampleNE.to('cpu').numpy()
            
            """
            if valid_NEs:
                sampleNE = sum(valid_NEs) / len(valid_NEs)
            else:
                sampleNE = 0
            """
            

            
            preds_all.append(preds)
            loss_all.append(loss.to('cpu'))
            acc_all.append(avg_acc.to('cpu'))
            NE_all.append(sampleNE)
            NEs = NEs.cpu().numpy()            
            
            results.append([joints_data['imgId'][0], loss.to('cpu').item(), NEs[0].item(), NEs[1].item(), NEs[2].item(),                                         NEs[3].item(), NEs[4].item(), NEs[5].item(), NEs[6].item(), NEs[7].item(), sampleNE[0]])
            
    
    preds_cols = ['imgId', 'x1', 'y1', 'x2', 'y2', 'x3', 'y3', 'x4', 'y4', 'x5', 'y5', 'x6', 'y6', 'x7', 'y7', 'x8', 'y8',                       'x1_pred', 'y1_pred', 'x2_pred', 'y2_pred', 'x3_pred','y3_pred', 'x4_pred', 'y4_pred', 'x5_pred', 'y5_pred', 'x6_pred', 'y6_pred', 'x7_pred', 'y7_pred','x8_pred', 'y8_pred', 'v1','v2','v3','v4','v5','v6','v7','v8']
    
    preds_df = pd.DataFrame(preds_all, columns=preds_cols)
    results_df_cols = ['imgId', 'MSEloss', 'NE1', 'NE2', 'NE3', 'NE4', 'NE5', 'NE6', 'NE7', 'NE8', 'NEavg']

    results_df = pd.DataFrame(results, columns=results_df_cols)
    mean_loss = np.average(loss_all)
    mean_acc = round(np.average(acc_all), 4)
    NEavg = round(np.average(NE_all), 4)

    # save results
    log_path = os.path.join(os.getcwd(), 'runs', 'val', datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(log_path, 0o755, exist_ok=False)  # exist_ok=False to avoid overwriting

    with open(os.path.join(log_path, 'parameters.json'), 'w') as f:
        json.dump(vars(opt), f,  indent=4)
    parameters = [str(vars(opt))]
    with open(os.path.join(log_path, 'parameters.txt'), 'w') as fd:
        fd.writelines(parameters)

    results_df.to_csv(os.path.join(log_path, 'results.csv'))
    preds_df.to_csv(os.path.join(log_path, 'preds.csv'))
    print('\n\n--------------------------------------------\nmean_loss: ', mean_loss)
    print(f'PCK@{pck_thr}: {mean_acc}')
    print(f'NEavg: {NEavg}')

    get_results_summary(log_path)

    print('\nTest ended @ %s' % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    
def parse_opt():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=str, default=None, help='./path/to/dataset')
    parser.add_argument('--weights', type=str, default=None, help='./weights/archived/Vis0_FDR_12k_earlystop.pth')
    parser.add_argument('--batch_size', type=int, default=1, help='batch size')
    parser.add_argument('--device', type=str, default=None, help='device')
    parser.add_argument('--pck_thr', type=float, default=0.05, help='pck threshold as a ratio of img diag')
    parser.add_argument('--vis_enabled', type=str, default='True', help='pck threshold as a ratio of img diag')
    opt = parser.parse_args()
    return opt

def main(opt):
    run(**vars(opt))

if __name__ == "__main__":
    opt = parse_opt()
    main(opt)
