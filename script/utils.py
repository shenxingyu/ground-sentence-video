"""
Some utility functions

Usage:
    utils.py extract-frames-tacos --visual-data-path=<dir> --processed-visual-data-path=<dir> --output-frame-size=<int>
    utils.py find-K --textual-data-path=<dir>
    utils.py extract-features --frames-path=<dir> --features-path=<dir>

"""


import torch
from torch.nn import Embedding
from gensim.models import KeyedVectors
from gensim.scripts.glove2word2vec import glove2word2vec
import numpy as np
from typing import Tuple, List, Dict
import os
import cv2
import math
import csv
from torchvision import transforms
from matplotlib import pyplot as plt
from skimage import transform
from tqdm import tqdm
from docopt import docopt
import sys
import torch.nn as nn
from models.cnn_encoder import VGG16


def pad_textual_data(sents: List[List[str]], pad_token):
    """ Pad list of sentences according to the longest sentence in the batch.
    :param sents: list of sentences, where each sentence is represented as a list of words
    :param pad_token: padding token
    :returns sents_padded: list of sentences where sentences shorter
    than the max length sentence are padded out with the pad_token, such that
    each sentences in the batch now has equal length.
    """
    longest = np.max([len(sent) for sent in sents])
    sents_padded = list(map(lambda sent: sent + [pad_token] * (longest - len(sent)), sents))

    return sents_padded


def pad_labels(labels: List[torch.Tensor]):
    """Pad labels according to the label with longest number of time steps (T)
    and concatenates them into a single torch.Tensor
    :param labels: a list with length num_labels of torch.Tensors
    :returns labels_padded: returns a torch.Tensor with shape (num_labels, T, K)
    """
    num_labels = len(labels)
    max_len = np.max([label.shape[0] for label in labels])
    K = labels[0].shape[1]
    labels_padded = torch.zeros([num_labels, max_len, K])

    for i in range(num_labels):
        labels_padded[i, :labels[i].shape[0], :] = labels[i]

    return labels_padded


def load_word_vectors(glove_file_path):
    print('Loading GloVE word vectors from {}...'.format(glove_file_path), file=sys.stderr)

    if not os.path.exists('glove.word2vec.txt'):
        glove2word2vec(glove_file_path, 'glove.word2vec.txt')

    model = KeyedVectors.load_word2vec_format('glove.word2vec.txt')
    words = list(model.vocab.keys())
    dim = len(model[words[0]])
    word_vectors = [np.zeros([2, dim])] + [model[word].reshape(1, -1) for word in words]
    word_vectors = np.concatenate(word_vectors, axis=0)

    return words, word_vectors


def extract_frames_tacos(visual_data_path: str, processed_visual_data_path: str, output_frame_size: Tuple):
    """Extracts frames from the raw videos of TACoS and save them as numpy arrays"""
    if not os.path.exists(processed_visual_data_path):
        os.mkdir(processed_visual_data_path)

    video_files = os.listdir(visual_data_path)

    for video_file in video_files:
        print('processing %s...' % video_file)
        cap = cv2.VideoCapture(os.path.join(visual_data_path, video_file))
        success = 1
        frames = []

        current_frame = 0
        fps = math.ceil(cap.get(cv2.CAP_PROP_FPS))

        while success:
            success, frame = cap.read()
            if success:
                if current_frame % (fps * 5) == 0:  # sampling one frame every five seconds
                    frame = transform.resize(frame, output_frame_size)  # resize the image
                    frames.append(np.expand_dims(frame, axis=0))
            else:
                break
            current_frame += 1

        frames = np.concatenate(frames).astype(np.float32)
        output_file = os.path.join(processed_visual_data_path, video_file.replace('.avi', '.npy'))
        np.save(output_file, frames)

        
def extract_visual_features(frames_path: str, features_path: str):
    """Extracts the features from frames using the pretrained VGG 16 network"""
    files = os.listdir(frames_path)
    
    # A standard transform needed to be applied to inputs of the models pre-trained on ImageNet
    transform_ = transforms.Compose([transforms.ToTensor(), 
                                     transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])])
    cnn_encoder = VGG16()
    device = 'cuda:0'
    cnn_encoder.to(device)
    
    for file in files:
        print('Extracting features of %s' % file)
        frames = np.load(os.path.join(preprocessed_visual_data_path, file))
        frames_tensor = torch.cat([transform_(frame).unsqueeze(dim=0) for frame in frames], dim=0)
        features = cnn_encoder(frames_tensor.to(device))
        out_file = os.path.join(features_path, file.replace('.npy', '_features.pt'))
        torch.save(features, out_file)


def find_bce_weights(dataset, K: int, device):
    """Finds the weights w0 and w1 used for computing the cross entropy loss
    (Please refer to the paper for details)
    """
    if not os.path.exists('w0_{}_{}.pt'.format(K, dataset.name)):
        print('Calculating BCE weights w0 and w1 for {}...'.format(dataset.name), file=sys.stderr)
        w0 = torch.zeros([K, ], dtype=torch.float32)

        num_samples = len(dataset)
        time_steps = 0

        for i in tqdm(range(num_samples)):
            _, _, label = dataset[i]
            T = label.shape[0]
            time_steps += T
            tmp = torch.sum(label, dim=0).to(torch.float32)
            w0 += T - tmp

        w0 = (w0 / time_steps).to(device)
        torch.save(w0, 'w0_{}_{}.pt'.format(K, dataset.name))
        
        return w0, 1-w0
    else:
        print('Loading BCE weights w0 and w1...', file=sys.stderr)
        
        w0 = torch.load('w0_{}_{}.pt'.format(K, dataset.name))
        w0 = w0.to(device)
        return w0, 1-w0


def top_n_iou(y_pred: torch.Tensor, gold_start_times: List[int], gold_end_times: List[int], args: Dict,
             fps: int, sample_rate: int):
    """Computes R@N, IOU=θ evaluation metric
    :param y_pred: torch.Tensor with shape (n_batch, T, K)
    :param gold_start_times: ground truth start frames with len (n_batch,)
    :param gold_end_times: ground truth end frames with len (n_batch,)
    :returns score: validation score
    """
    n_batch, T, K = y_pred.shape

    delta = int(args['--delta'])
    threshold = float(args['--threshold'])

    # computing indices which is a Tensor with shape (n_batch, top_n_eval)
    _, indices = torch.topk(y_pred.view(n_batch, -1), k=int(args['--top-n-eval']), dim=-1)

    end_times = (indices // K) * sample_rate / fps  # tensor with shape (n_batch, top_n_eval)
    scale_nums = (indices % K) + 1
    start_times = end_times - (scale_nums * delta * sample_rate / fps)

    score = 0

    for i in range(n_batch):
        max_overlap = np.max([compute_overlap(start_time.item(), end_time.item(), gold_start_times[i],
                                              gold_end_times[i])
                              for start_time, end_time in zip(start_times[i], end_times[i])])
        score += int(max_overlap > threshold)

    return score


def find_K(textual_data_path: str):
    """Shows the statistics of the textual data in order to find the appropriate valud of K
    (Please refer to the paper for more details)
    """
    lengths = []
    for file in os.listdir(textual_data_path):
        with open(os.path.join(textual_data_path, file)) as tsvfile:
            reader = csv.reader(tsvfile, delimiter='\t')
            for row in reader:
                start_frame, end_frame = int(row[0]), int(row[1])
                lengths.append(end_frame - start_frame)

    print(np.mean(lengths))
    print(np.sort(lengths))
    plt.hist(lengths)
    plt.show()


def compute_overlap(start_a: float, end_a: float, start_b: float, end_b: float):
    """Computes the temporal overlap between two segments
    :param start_a: start time of first segment
    :param end_a: end frame of first segment
    :param start_b: start frame of second segment
    :param end_b: end frame of second segment
    :returns the temporal overlap between the segments (float)
    """
    if end_a < start_b or end_b < start_a:
        return 0

    if start_a <= start_b:
        if start_b <= end_a <= end_b:
            return end_a - start_b
        elif end_a > end_b:
            return end_b - start_b
    else:
        if start_a <= end_b <= end_a:
            return end_b - start_a
        elif end_b > end_a:
            return end_a - start_a


if __name__ == '__main__':
    args = docopt(__doc__)
    if args['process-visual-data-tacos']:
        visual_data_path = args['--visual-data-path']
        processed_visual_data_path = args['--processed-visual-data-path']
        output_frame_size = int(args['--output-frame-size'])
        process_visual_data_tacos(visual_data_path, processed_visual_data_path, (output_frame_size, output_frame_size))
    elif args['extract-features']:
        extract_features(args['--preprocessed-visual-data-path'], args['--features-path'])
    elif args['find-K']:
        find_K(args['--textual-data-path'])
