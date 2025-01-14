import torch.nn as nn
import torch.nn.functional as F
import torch
from torch.autograd import Variable
import random
import pandas as pd
import torch
import time
import numpy as np
import warnings
from gensim.models.word2vec import Word2Vec
from torch.autograd import Variable
from sklearn.metrics import precision_recall_fscore_support
from utilities import SharedFunction
# warnings.filterwarnings('ignore')

class BatchTreeEncoder(nn.Module):
    def __init__(self, vocab_size, embedding_dim, encode_dim, batch_size, use_gpu, pretrained_weight=None):
        super(BatchTreeEncoder, self).__init__()
        self.embedding = nn.Embedding(vocab_size, embedding_dim)
        self.embedding_dim = embedding_dim
        self.encode_dim = encode_dim
        self.W_c = nn.Linear(embedding_dim, encode_dim)
        self.activation = F.relu
        self.stop = -1
        self.batch_size = batch_size
        self.use_gpu = use_gpu
        self.node_list = []
        self.th = torch.cuda if use_gpu else torch
        self.batch_node = None
        self.max_index = vocab_size
        # pretrained  embedding
        if pretrained_weight is not None:
            self.embedding.weight.data.copy_(torch.from_numpy(pretrained_weight))
            # self.embedding.weight.requires_grad = False

    def create_tensor(self, tensor):
        if self.use_gpu:
            return tensor.cuda()
        return tensor

    def traverse_mul(self, node, batch_index):
        size = len(node)
        if not size:
            return None
        batch_current = self.create_tensor(Variable(torch.zeros(size, self.embedding_dim)))

        index, children_index = [], []
        current_node, children = [], []
        for i in range(size):
            # if node[i][0] is not -1:
                index.append(i)
                current_node.append(node[i][0])
                temp = node[i][1:]
                c_num = len(temp)
                for j in range(c_num):
                    if temp[j][0] != -1:
                        if len(children_index) <= j:
                            children_index.append([i])
                            children.append([temp[j]])
                        else:
                            children_index[j].append(i)
                            children[j].append(temp[j])
            # else:
            #     batch_index[i] = -1

        batch_current = self.W_c(batch_current.index_copy(0, Variable(self.th.LongTensor(index)),
                                                          self.embedding(Variable(self.th.LongTensor(current_node)))))

        for c in range(len(children)):
            zeros = self.create_tensor(Variable(torch.zeros(size, self.encode_dim)))
            batch_children_index = [batch_index[i] for i in children_index[c]]
            tree = self.traverse_mul(children[c], batch_children_index)
            if tree is not None:
                batch_current += zeros.index_copy(0, Variable(self.th.LongTensor(children_index[c])), tree)
        # batch_index = [i for i in batch_index if i is not -1]
        b_in = Variable(self.th.LongTensor(batch_index))
        self.node_list.append(self.batch_node.index_copy(0, b_in, batch_current))
        return batch_current

    def forward(self, x, bs):
        self.batch_size = bs
        self.batch_node = self.create_tensor(Variable(torch.zeros(self.batch_size, self.encode_dim)))
        self.node_list = []
        self.traverse_mul(x, list(range(self.batch_size)))
        self.node_list = torch.stack(self.node_list)
        return torch.max(self.node_list, 0)[0]
    
class BatchProgramClassifier(nn.Module):
    def __init__(self, embedding_dim, hidden_dim, vocab_size, encode_dim, label_size, batch_size,
                 use_gpu=True, pretrained_weight=None):
        super(BatchProgramClassifier, self).__init__()
        self.additionl = nn.Linear(hidden_dim * 4, hidden_dim * 2)
        self.additionr = nn.Linear(hidden_dim * 4, hidden_dim * 2)
        self.stop = [vocab_size-1]
        self.hidden_dim = hidden_dim
        self.num_layers = 1
        self.gpu = use_gpu
        self.batch_size = batch_size
        self.vocab_size = vocab_size
        self.embedding_dim = embedding_dim
        self.encode_dim = encode_dim
        self.label_size = label_size
        self.encoder = BatchTreeEncoder(self.vocab_size, self.embedding_dim, self.encode_dim,
                                        self.batch_size, self.gpu, pretrained_weight)
        self.root2label = nn.Linear(self.encode_dim, self.label_size)
        # gru
        self.bigru = nn.GRU(self.encode_dim, self.hidden_dim, num_layers=self.num_layers, bidirectional=True,
                            batch_first=True)
        # linear
        self.hidden2label = nn.Linear(self.hidden_dim * 2 + 1, self.label_size) # version history + callgraph + number of days/versions
        # hidden
        self.hidden = self.init_hidden()
        self.dropout = nn.Dropout(0.2)

    def init_hidden(self):
        if self.gpu is True:
            if isinstance(self.bigru, nn.LSTM):
                h0 = Variable(torch.zeros(self.num_layers * 2, self.batch_size, self.hidden_dim).cuda())
                c0 = Variable(torch.zeros(self.num_layers * 2, self.batch_size, self.hidden_dim).cuda())
                return h0, c0
            return Variable(torch.zeros(self.num_layers * 2, self.batch_size, self.hidden_dim)).cuda()
        else:
            return Variable(torch.zeros(self.num_layers * 2, self.batch_size, self.hidden_dim))

    def get_zeros(self, num):
        zeros = Variable(torch.zeros(num, self.encode_dim))
        if self.gpu:
            return zeros.cuda()
        return zeros

    def encode(self, x):
        lens = [len(item) for item in x]
        max_len = max(lens)

        encodes = []
        for i in range(self.batch_size):
            for j in range(lens[i]):
                encodes.append(x[i][j])

        encodes = self.encoder(encodes, sum(lens))
        seq, start, end = [], 0, 0
        for i in range(self.batch_size):
            end += lens[i]
            if max_len-lens[i]:
                seq.append(self.get_zeros(max_len-lens[i]))
            seq.append(encodes[start:end])
            start = end
        encodes = torch.cat(seq)
        encodes = encodes.view(self.batch_size, max_len, -1)
        # return encodes

        gru_out, hidden = self.bigru(encodes, self.hidden)
        gru_out = torch.transpose(gru_out, 1, 2)
        # pooling
        gru_out = F.max_pool1d(gru_out, gru_out.size(2)).squeeze(2)
        # gru_out = gru_out[:,-1]

        return gru_out
    
    def encode_number(self, x):
        return torch.Tensor(np.array(x)).cuda()
    
    def merge_versions(self, x):
        # merge all versions' embedding vectors into one long vector, then encode it with ASTNN architecture
        x_merged = []
        for cell in x:
            l = []
            for v in cell:
                l += v
            x_merged.append(l)
        return x_merged

    def forward(self, x1, x2, x3, x4, x5, x6, x7, x8, x9, x10, y1, y2, y3, y4, y5, y6, y7, y8, y9, y10):
    
        l_code, r_code = self.encode(x1), self.encode(y1)
        
        l_calling, r_calling = self.encode(x3), self.encode(y3)
        l_called, r_called = self.encode(x4), self.encode(y4)
        l_number_of_days, r_number_of_days = self.encode_number(x8), self.encode_number(y8)
        # l_number_of_vers, r_number_of_vers = self.encode_number(x9), self.encode_number(y9)
        l_code_versions_all, r_code_versions_all = self.encode(self.merge_versions(x10)), self.encode(self.merge_versions(y10))

        # l_diff = torch.add(l_calling, -l_called)
        # r_diff = torch.add(r_calling, -r_called)

        # r = self.context_weight
        # lvec = torch.add((1 - r) * l_code, r * l_diff)
        # rvec = torch.add((1 - r) * r_code, r * r_diff)

        l_code = torch.cat([l_code, l_number_of_days], 1)
        r_code = torch.cat([r_code, r_number_of_days], 1)
        l_calling = torch.cat([l_calling, l_number_of_days], 1)
        r_calling = torch.cat([r_calling, r_number_of_days], 1)
        l_called = torch.cat([l_called, l_number_of_days], 1)
        r_called = torch.cat([r_called, r_number_of_days], 1)
        l_code_versions_all = torch.cat([l_code_versions_all, l_number_of_days], 1)
        r_code_versions_all = torch.cat([r_code_versions_all, r_number_of_days], 1)

        # breakpoint()

        # l_number_of_days = l_number_of_days.expand(l_code.size(0), l_code.size(1))
        # r_number_of_days = l_number_of_days.expand(r_code.size(0), r_code.size(1))

        lvec = torch.cat([l_code, l_code_versions_all, l_calling, l_called], 0)
        rvec = torch.cat([r_code, r_code_versions_all, r_calling, r_called], 0)
        # lvec = torch.cat([l_code, l_calling, l_called], 0)
        # rvec = torch.cat([r_code, r_calling, r_called], 0)
        
        # lvec = lvec.view(2, self.batch_size, self.hidden_dim * 2 + 1)
        # rvec = rvec.view(2, self.batch_size, self.hidden_dim * 2 + 1)
        # lvec = lvec.view(3, self.batch_size, self.hidden_dim * 2)
        # rvec = rvec.view(3, self.batch_size, self.hidden_dim * 2)
        lvec = lvec.view(4, self.batch_size, self.hidden_dim * 2 + 1)
        rvec = rvec.view(4, self.batch_size, self.hidden_dim * 2 + 1)
        
        lvec = torch.max(lvec, dim=0).values
        rvec = torch.max(rvec, dim=0).values
        # lvec = self.additionl(lvec)
        # rvec = self.additionr(rvec)

        abs_dist = torch.abs(torch.add(lvec, -rvec))
        # contexted_dis = torch.cat([abs_dist, l_calling, r_calling, l_called, r_called], 1)

        y = self.hidden2label(abs_dist)
        return y
    
def get_batch(dataset, idx, bs):
    tmp = dataset.iloc[idx: idx+bs]
    x1, x2, x3, x4, x5, x6, x7, x8, x9, x10, y1, y2, y3, y4, y5, y6, y7, y8, y9, y10, labels = [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []
    # x1, x2, x8, x9, y1, y2, y8, y9, labels = [], [], [], [], [], [], [], [], [], 
    for _, item in tmp.iterrows():
        x1.append(item['code_x'])
        x2.append(item['code_versions_x'])
        x3.append(item['calling_x'])
        x4.append(item['called_x'])
        x5.append(item['code_v1_x'])
        x6.append(item['calling_v1_x'])
        x7.append(item['called_v1_x'])
        x8.append(item['number_of_days_x'])
        x9.append(item['number_of_versions_x'])
        x10.append(item['code_versions_all_x'])

        y1.append(item['code_y'])
        y2.append(item['code_versions_y'])
        y3.append(item['calling_y'])
        y4.append(item['called_y'])
        y5.append(item['code_v1_y'])
        y6.append(item['calling_v1_y'])
        y7.append(item['called_v1_y'])
        y8.append(item['number_of_days_y'])
        y9.append(item['number_of_versions_y'])
        y10.append(item['code_versions_all_y'])

        labels.append([item['label']])
    return x1, x2, x3, x4, x5, x6, x7, x8, x9, x10, y1, y2, y3, y4, y5, y6, y7, y8, y9, y10, torch.FloatTensor(labels)
    # return x1, x2, x8, x9, y1, y2, y8, y9, torch.FloatTensor(labels)


# def get_context_batch(dataset, idx, bs):
#     tmp = dataset.iloc[idx: idx + bs]
#     x1, x2, x3, x4, x5, x6, x7, y1, y2, y3, y4, y5, y6, y7, labels = [], [], [], [], [], [], [], [], [], [], [], [], [], [], []
#     # x1, x3, x4, y1, y3, y4, labels = [], [], [], [], [], [], []
#     for _, item in tmp.iterrows():
#         x1.append(item['code_x'])
#         x2.append(item['code_versions_x'])
#         x3.append(item['calling_x'])
#         x4.append(item['called_x'])

#         x5.append(item['code_v1_x'])
#         x6.append(item['calling_v1_x'])
#         x7.append(item['called_v1_x'])

#         y1.append(item['code_y'])
#         y2.append(item['code_versions_y'])
#         y3.append(item['calling_y'])
#         y4.append(item['called_y'])

#         y5.append(item['code_v1_y'])
#         y6.append(item['calling_v1_y'])
#         y7.append(item['called_v1_y'])

#         labels.append([item['label']])
#     return x1, x2, x3, x4, x5, x6, x7, y1, y2, y3, y4, y5, y6, y7, torch.FloatTensor(labels)
#     # return x1, x3, x4, y1, y3, y4, torch.FloatTensor(labels)

if __name__ == '__main__':
    RANDOM_SEED = 2023
    DATA_DIR = './data/clone_detection'
    MODEL_DIR = './models'

    word2vec = Word2Vec.load(DATA_DIR + '/node_w2v_128').wv
    MAX_TOKENS = word2vec.vectors.shape[0]
    EMBEDDING_DIM = word2vec.vectors.shape[1]
    embeddings = np.zeros((MAX_TOKENS + 1, EMBEDDING_DIM), dtype="float32")
    embeddings[:word2vec.vectors.shape[0]] = word2vec.vectors

    HIDDEN_DIM = 100
    ENCODE_DIM = 128
    LABELS = 1
    EPOCHS = 20
    BATCH_SIZE = 2
    USE_GPU = True

    torch.manual_seed(RANDOM_SEED)

    print("Train for clone detection - MAX POOLING - With VERSION ALL + CALLGRAPH + NUM OF DAYS ")
    train_data = pd.read_pickle(DATA_DIR + '/train_blocks.pkl').sample(frac=1, random_state=RANDOM_SEED)
    dev_data = pd.read_pickle(DATA_DIR + '/dev_blocks.pkl').sample(frac=1, random_state=RANDOM_SEED)
    test_data = pd.read_pickle(DATA_DIR + '/test_blocks.pkl').sample(frac=1, random_state=RANDOM_SEED)

    train_data.loc[train_data['label'] > 0, 'label'] = 1
    dev_data.loc[dev_data['label'] > 0, 'label'] = 1
    test_data.loc[test_data['label'] > 0, 'label'] = 1
    model = BatchProgramClassifier(EMBEDDING_DIM, HIDDEN_DIM, MAX_TOKENS+1, ENCODE_DIM, LABELS, BATCH_SIZE,
                                   USE_GPU, embeddings)

    if USE_GPU:
        model.cuda()

    parameters = model.parameters()
    optimizer = torch.optim.Adamax(parameters, lr=0.003)
    loss_function = torch.nn.BCEWithLogitsLoss()

    # print(train_data)
    precision, recall, f1 = 0, 0, 0
    print('Start training...')

    # training procedure
    # state_dict = torch.load(MODEL_DIR + '/clone_max_pool.pth')
    # model.load_state_dict(state_dict)
    best_loss = 10
    best_model = None
    for epoch in range(EPOCHS):
        start_time = time.time()
        # training epoch
        total_acc = 0.0
        total_loss = 0.0
        total = 0.0
        i = 0
        while i < len(train_data):
            batch = get_batch(train_data, i, BATCH_SIZE)
            i += BATCH_SIZE
            # train_code_x, train_code_versions_x, train_calling_x, train_called_x, train_code_y, train_code_versions_y, train_calling_y, train_called_y, train_labels = batch
            train_code_x, train_code_versions_x, train_calling_x, train_called_x, train_code_v1_x, train_calling_v1_x, train_called_v1_x, train_number_of_days_x, train_number_of_versions_x, train_code_versions_all_x, \
                train_code_y, train_code_versions_y, train_calling_y, train_called_y, train_code_v1_y, train_calling_v1_y, train_called_v1_y, train_number_of_days_y, train_number_of_versions_y, train_code_versions_all_y, \
                    train_labels = batch
            if USE_GPU:
                # train1_inputs, train2_inputs, train_labels = train1_inputs, train2_inputs, train_labels.cuda()
                train_labels = train_labels.cuda()

            model.zero_grad()
            model.batch_size = len(train_labels)
            model.hidden = model.init_hidden()            
            # output = model(train_code_x, train_code_versions_x, train_calling_x, train_called_x, train_code_y, train_code_versions_y, train_calling_y, train_called_y)
            output = model(
                train_code_x, train_code_versions_x, train_calling_x, train_called_x, train_code_v1_x, train_calling_v1_x, train_called_v1_x, train_number_of_days_x, train_number_of_versions_x, train_code_versions_all_x, 
                train_code_y, train_code_versions_y, train_calling_y, train_called_y, train_code_v1_y, train_calling_v1_y, train_called_v1_y, train_number_of_days_y, train_number_of_versions_y, train_code_versions_all_y
                )

            # train_labels = train_labels.squeeze()
            loss = loss_function(output, train_labels.float())
            loss.backward()
            optimizer.step()

            # output = output.squeeze()
            output = torch.sigmoid(output)
            predicted = torch.round(output)
            for idx in range(len(predicted)):
                if predicted[idx] == train_labels[idx]:
                    total_acc += 1
            total += len(train_labels)
            total_loss += loss.item() * len(train_labels)
        train_loss = total_loss / total
        train_acc = total_acc / total

        # dev epoch
        total_acc = 0.0
        total_loss = 0.0
        total = 0.0
        i = 0
        while i < len(dev_data):
            batch = get_batch(dev_data, i, BATCH_SIZE)
            i += BATCH_SIZE
            # dev_code_x, dev_code_version_x, dev_calling_x, dev_called_x, dev_code_y, dev_code_version_y, dev_calling_y, dev_called_y, dev_labels = batch
            dev_code_x, dev_code_versions_x, dev_calling_x, dev_called_x, dev_code_v1_x, dev_calling_v1_x, dev_called_v1_x, dev_number_of_days_x, dev_number_of_versions_x, dev_code_versions_all_x, \
                dev_code_y, dev_code_versions_y, dev_calling_y, dev_called_y, dev_code_v1_y, dev_calling_v1_y, dev_called_v1_y, dev_number_of_days_y, dev_number_of_versions_y, dev_code_versions_all_y, \
                    dev_labels = batch
            # val_inputs, val_labels = batch
            if USE_GPU:
                # val_inputs, val_labels = val_inputs, val_labels.cuda()
                dev_labels = dev_labels.cuda()

            model.batch_size = len(dev_labels)
            model.hidden = model.init_hidden()
            # output = model(dev_code_x, dev_code_version_x, dev_calling_x, dev_called_x, dev_code_y, dev_code_version_y, dev_calling_y, dev_called_y)
            output = model(
                dev_code_x, dev_code_versions_x, dev_calling_x, dev_called_x, dev_code_v1_x, dev_calling_v1_x, dev_called_v1_x, dev_number_of_days_x, dev_number_of_versions_x, dev_code_versions_all_x, 
                dev_code_y, dev_code_versions_y, dev_calling_y, dev_called_y, dev_code_v1_y, dev_calling_v1_y, dev_called_v1_y, dev_number_of_days_y, dev_number_of_versions_y, dev_code_versions_all_y
                )

            # dev_labels = dev_labels.squeeze()
            loss = loss_function(output, dev_labels.float())

            # output = output.squeeze()
            output = torch.sigmoid(output)
            predicted = torch.round(output)
            for idx in range(len(predicted)):
                if predicted[idx] == dev_labels[idx]:
                    total_acc += 1
            total += len(dev_labels)
            total_loss += loss.item() * len(dev_labels)
        epoch_loss = total_loss / total
        epoch_acc = total_acc / total
        end_time = time.time()
        if epoch_loss < best_loss:
            best_loss = epoch_loss
            best_model = model
        print('[Epoch: %3d/%3d] Train Loss: %.4f, Validation Loss: %.4f, '
              'Train Acc: %.3f, Validation Acc: %.3f, Time Cost: %.3f s'
              % (epoch + 1, EPOCHS, train_loss, epoch_loss, train_acc,
                 epoch_acc, end_time - start_time))

    model = best_model
    torch.save(model.state_dict(), MODEL_DIR + '/clone_max_pool.pth')

    """
    test
    """
    # model = BatchProgramClassifier(EMBEDDING_DIM, HIDDEN_DIM, MAX_TOKENS + 1, ENCODE_DIM, LABELS, BATCH_SIZE,
    #                                USE_GPU, embeddings)
    # model.load_state_dict(torch.load(MODEL_DIR + '/clone_max_pool.pth'))

    if USE_GPU:
        model.cuda()

    # testing procedure
    predicts = []
    trues = []
    total_loss = 0.0
    total_acc = 0
    total = 0.0
    i = 0
    while i < len(test_data):
        batch = get_batch(test_data, i, BATCH_SIZE)
        i += BATCH_SIZE
        # test1_inputs, test2_inputs, test_labels = batch
        # test_code_x, test_code_version_x, test_calling_x, test_called_x, test_code_y, test_code_version_y, test_calling_y, test_called_y, test_labels = batch
        test_code_x, test_code_versions_x, test_calling_x, test_called_x, test_code_v1_x, test_calling_v1_x, test_called_v1_x, test_number_of_days_x, test_number_of_versions_x, test_code_versions_all_x, \
                test_code_y, test_code_versions_y, test_calling_y, test_called_y, test_code_v1_y, test_calling_v1_y, test_called_v1_y, test_number_of_days_y, test_number_of_versions_y, test_code_versions_all_y, \
                    test_labels = batch
        if USE_GPU:
            test_labels = test_labels.cuda()

        model.batch_size = len(test_labels)
        model.hidden = model.init_hidden()
        # output = model(test_code_x, test_code_version_x, test_calling_x, test_called_x, test_code_y, test_code_version_y, test_calling_y, test_called_y)
        output = model(
                test_code_x, test_code_versions_x, test_calling_x, test_called_x, test_code_v1_x, test_calling_v1_x, test_called_v1_x, test_number_of_days_x, test_number_of_versions_x, test_code_versions_all_x, 
                test_code_y, test_code_versions_y, test_calling_y, test_called_y, test_code_v1_y, test_calling_v1_y, test_called_v1_y, test_number_of_days_y, test_number_of_versions_y, test_code_versions_all_y
                )

        output = torch.sigmoid(output)
        predicted = torch.round(output)
        for idx in range(len(predicted)):
            if predicted[idx] == test_labels[idx]:
                total_acc += 1
        total += len(test_labels)
        #         predicted = (output.data > 0.5).cpu().numpy()
        predicts.extend(predicted.cpu().detach().numpy())
        trues.extend(test_labels.cpu().numpy())

    acc = total_acc / total
    print("total accuracy: ", total_acc)
    p, r, f, _ = precision_recall_fscore_support(trues, predicts, average='binary')

    print("Total testing results(acc,P,R,F1):%.5f, %.5f, %.5f, %.5f" % (acc, p, r, f))

    # store model result    
    model_log = "ASTNN-VersionAll_CallGraph_NumOfDays-Clone Max Pool, %.5f, %.5f, %.5f, %.5f" % (acc, p, r, f)
    obj = SharedFunction(model_log)
    obj.AppendFile()