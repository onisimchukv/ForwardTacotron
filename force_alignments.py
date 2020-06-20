import numpy as np
import torch
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph._shortest_path import dijkstra
from scipy.sparse.csr import csr_matrix
import torch

from models.aligner import Aligner
from utils.dataset import filter_max_len
from utils.files import unpickle_binary
from utils.paths import Paths
from utils.text import phonemes, text_to_sequence, sequence_to_text
from utils.text.cleaners import german_cleaners
from utils import hparams as hp

hp.configure('hparams.py')  # Load hparams from file

text_dict = unpickle_binary('data/text_dict.pkl')

if torch.cuda.is_available():
    device = torch.device('cuda')
else:
    device = torch.device('cpu')
print('Using device:', device)

model = Aligner(n_mels=80, lstm_dim=256, num_symbols=len(phonemes)).to(device)
model.load('checkpoints/asvoice_newdurs_tts.aligner/latest_weights.pyt')
model.eval()
paths = Paths(hp.data_path, hp.voc_model_id, hp.tts_model_id)
print(f'loaded aligner step {model.get_step()}')

val_data = unpickle_binary('data/val_dataset.pkl')
train_ids, train_lens = filter_max_len(val_data)
val_ids, val_lens = filter_max_len(val_data)

for num_id, id in enumerate(val_ids):
    print(f'predict {id}, {num_id}/{len(text_dict)}')
    mel = np.load(f'data/mel/{id}.npy')

    mel = torch.tensor(mel).to(device)
    text = text_dict[id]
    seq = text_to_sequence(text)
    seq = torch.tensor(seq)
    pred = model(mel.unsqueeze(0).transpose(1, 2))
    pred = torch.log_softmax(pred, dim=-1)
    pred = pred.detach()[0].cpu().numpy()
    target = seq.numpy()

    target_len = target.shape[0]
    pred_len = pred.shape[0]
    pred_max = np.zeros((pred_len, target_len))

    for i in range(pred_len):
        weight = - pred[i, target]
        pred_max[i] = weight

    def to_node_index(i, j, cols):
        return cols * i + j

    def from_node_index(node_index, cols):
        return node_index // cols, node_index % cols

    def to_adj_matrix(mat):
        rows = mat.shape[0]
        cols = mat.shape[1]

        row_ind = []
        col_ind = []
        data = []

        for i in range(rows):
            for j in range(cols):

                node = to_node_index(i, j, cols)

                if j < cols - 1:
                    right_node = to_node_index(i, j + 1, cols)
                    weight_right = mat[i, j + 1]
                    row_ind.append(node)
                    col_ind.append(right_node)
                    data.append(weight_right)

                if i < rows -1:
                    bottom_node = to_node_index(i + 1, j, cols)
                    weight_bottom = mat[i + 1, j]
                    row_ind.append(node)
                    col_ind.append(bottom_node)
                    data.append(weight_bottom)

        #print(f'max row_ind {max(row_ind)} max col_ind {max(col_ind)} dim {ro}')
        adj_mat = coo_matrix((data, (row_ind, col_ind)), shape=(rows * cols, rows * cols))
        return adj_mat.tocsr()

    adj_matrix = to_adj_matrix(pred_max)

    dist_matrix, predecessors = dijkstra(csgraph=adj_matrix, directed=True, indices=0, return_predecessors=True)

    path = []
    pr_index = predecessors[-1]
    while pr_index != 0:
        path.append(pr_index)
        pr_index = predecessors[pr_index]
    path.reverse()
    # append first and last
    path = [0] + path + [dist_matrix.size-1]
    cols = pred_max.shape[1]

    mel_text = {}

    text_mel = {}
    text_mel_prob = {}

    durations = np.zeros(seq.shape[0])
    durations_new = np.zeros(seq.shape[0])

    for node_index in path:
        i, j = from_node_index(node_index, cols)

        k = target[j]
        prob = pred[i, k]
        tm_prob = text_mel_prob.get(j, -1e10)
        if prob > tm_prob:
            text_mel[j] = i
            text_mel_prob[j] = prob

    for node_index in path:
        i, j = from_node_index(node_index, cols)

        prob = pred[i, j]
        tm_prob = text_mel_prob.get(j, -1e10)
        if prob > tm_prob:
            text_mel[j] = i
            text_mel_prob[j] = prob

        letter = sequence_to_text([target[j]])
        pred_letter = sequence_to_text([np.argmax(pred[i], axis=-1)])
        #print(f'{i} {j} {letter} {pred_letter} {pred_max[i, j]}')
        mel_text[i] = j



    for j in mel_text.values():
        durations[j] += 1

    # correct for missing durs
    for i in range(len(durations)):
        if durations[i] == 0:
            left_dur = 0
            right_dur = 0
            if i > 0: left_dur = durations[i-1]
            if i < len(durations) - 1: right_dur = durations[i+1]
            if left_dur > right_dur and left_dur > 1:
                durations[i] += 1
                durations[i - 1] -= 1
            if right_dur > left_dur and right_dur > 1:
                durations[i] += 1
                durations[i + 1] -= 1

    sum_durs = 0
    for j in range(len(text_mel) - 1):
        durations_new[j] = (text_mel[j] + text_mel[j + 1]) // 2 - sum_durs
        sum_durs += durations_new[j]
    durations_new[-1] = len(mel_text) - sum(durations_new)

    print(text)
    print(durations)
    print(f'sum durs: {sum(durations)} mel shape {mel.shape}')
    print(f'sum durs new: {sum(durations_new)} mel shape {mel.shape}')

    np.save(paths.alg/f'{id}.npy', np.array(durations))
    np.save(paths.alg2/f'{id}.npy', np.array(durations_new))
