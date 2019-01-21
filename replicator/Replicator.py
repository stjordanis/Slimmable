from abc import abstractmethod
from math import floor
from multiprocessing.pool import Pool

from torch import tensor, no_grad
from torch.cuda import set_device

from trainRegimes.regime import TrainRegime
from models.BaseNet.BaseNet import BaseNet
from .Replica import Replica


# from multiprocessing import Process
# class NoDaemonProcess(Process):
#     # make 'daemon' attribute always return False
#     def _get_daemon(self):
#         return False
#
#     def _set_daemon(self, value):
#         pass
#
#     daemon = property(_get_daemon, _set_daemon)
#
#
# # We sub-class multiprocessing.pool.Pool instead of multiprocessing.Pool
# # because the latter is only a wrapper function, not a proper class.
# class MyPool(Pool):
#     Process = NoDaemonProcess

# # usage example
# with MyPool(processes=nCopies, maxtasksperchild=1) as p:


class ModelReplicator:
    title = 'Replications'

    def __init__(self, regime: TrainRegime):
        self._regime = regime
        self._model = regime.model
        self.gpuIDs = regime.args.gpu
        # self._modelStateDict = regime.model.state_dict()
        # self._modelAlphas = regime.model.alphas()
        self._modelStateDict = self._cloneModelStateDict(self._model.state_dict())
        # self._modelAlphas = self._cloneModelAlphas(regime.model.alphas())

        # # save current device
        # currDevice = current_device()
        # # create replications
        # self.replications = [Replica(regime, gpu) for gpu in self.gpuIDs]
        # # reset device back to current device
        # set_device(currDevice)

        # create info table
        regime.logger.addInfoTable(self.title, [['#', len(self.gpuIDs)]])

    @abstractmethod
    def processResults(self, results: list) -> (list, list):
        raise NotImplementedError('subclasses must override processResults()!')

    def _cloneModelStateDict(self, modelStateDict: dict):
        gpuStateDictClones = {}
        for gpu in self.gpuIDs:
            # init model state_dict clone for GPU gpu
            stateDictClone = {}
            # fill model state_dict clone with tensors on GPU gpu
            for k, v in modelStateDict.items():
                stateDictClone[k] = v if (v.device.index == gpu) else v.clone().cuda(gpu)
            # add model state_dict clone on GPU gpu to GPUs dictionary
            gpuStateDictClones[gpu] = stateDictClone

        return gpuStateDictClones

    def _cloneModelAlphas(self, modelAlphas: list):
        gpuAlphasClones = {}
        for gpu in self.gpuIDs:
            # init model state_dict clone for GPU gpu
            alphasClone = [t.detach() if (t.device.index == gpu) else t.detach().clone().cuda(gpu) for t in modelAlphas]
            # add model alphas clone on GPU gpu to GPUs dictionary
            gpuAlphasClones[gpu] = alphasClone

        return gpuAlphasClones

    # def initNewEpoch(self):
    #     for replica in self.replications:
    #         replica.initNewEpoch(self._modelStateDict)

    # update source model weights & alphas values
    def initNewEpoch(self, srcModel: BaseNet):
        # self._modelStateDict = srcModel.state_dict()
        # self._modelAlphas = srcModel.alphas()
        # self._modelAlphas = self._cloneModelAlphas(srcModel.alphas())
        # update replications weights source
        self._modelStateDict = self._cloneModelStateDict(srcModel.state_dict())

    # given paths history dictionary and current path, checks if current path exists in history dictionary
    @staticmethod
    def _doesPathExist(pathsHistoryDict: dict, currPath: list) -> bool:
        currDict = pathsHistoryDict
        # init boolean flag
        pathExists = True
        for v in currPath:
            if v not in currDict:
                pathExists = False
                currDict[v] = {}
            # update current dict, we move to next layer
            currDict = currDict[v]

        return pathExists

    # select new path based on alphas distribution.
    # check that selected path hasn't been selected before
    @staticmethod
    def _generateNewPath(replica: Replica, pathsHistoryDict: dict) -> list:
        cModel = replica.getModel()
        # restore model original weights & BNs
        replica.restoreModelOriginalWeights()
        # init does path exist flag
        pathExists = True
        while pathExists:
            # select path based on alphas distribution
            cModel.choosePathByAlphas()
            # get selected path indices
            pathWidthIdx = cModel.currWidthIdx()
            # check that selected path hasn't been selected before
            pathExists = ModelReplicator._doesPathExist(pathsHistoryDict, pathWidthIdx)

        print('pathWidthIdx:{}'.format(pathWidthIdx))
        return pathWidthIdx

    def _splitSamples(self, nSamples: int) -> dict:
        nCopies = len(self.gpuIDs)
        # split number of samples between model replications
        nSamplesPerCopy = {gpu: floor(nSamples / nCopies) for gpu in self.gpuIDs}
        # calc difference between nSamples to samples assigned to copies
        diff = nSamples - sum(nSamplesPerCopy.values())
        # split difference evenly between copies
        for idx in range(diff):
            nSamplesPerCopy[self.gpuIDs[idx]] += 1

        assert (sum(nSamplesPerCopy.values()) == nSamples)
        return nSamplesPerCopy

    def _updateReplicationsAlphas(self):
        for replica in self.replications:
            cModel = replica.cModel()
            cModel.updateAlphas(self._modelAlphas)

    def _cloneTensors(self, tensorsList: list) -> dict:
        dataPerGPU = {}
        for gpu in self.gpuIDs:
            gpuTensorsList = [t if (gpu == t.device.index) else t.clone().cuda(gpu) for t in tensorsList]
            dataPerGPU[gpu] = gpuTensorsList

        return dataPerGPU

    def loss(self, input: tensor, target: tensor, nSamples: int):
        # clone input & target to all GPUs
        dataPerGPU = self._cloneTensors([input, target])
        # clone model alphas tensors
        modelAlphas = self._cloneModelAlphas(self._model.alphas())
        # split samples between model copies
        nSamplesPerModel = self._splitSamples(nSamples)
        # init loss replication function arguments
        args = self.buildArgs(dataPerGPU, modelAlphas, nSamplesPerModel)

        nCopies = len(self.gpuIDs)
        with Pool(processes=nCopies, maxtasksperchild=1) as p:
            # results = p.map(self.lossPerReplication, args)
            results = p.map(self.lossPerReplication, args)

        return self.processResults(results)

    def buildArgs(self, dataPerGPU: dict, modelAlphas: dict, nSamplesPerCopy: int):
        # args = ((self._regime, dataPerGPU[gpu], nSamplesPerCopy[gpu], gpu) for gpu in self.gpuIDs)
        regime = self._regime
        args = ((regime.buildModel, regime.flopsLoss, self._modelStateDict[gpu], modelAlphas[gpu], self.initPathsList(), self.initLossDictsList(),
                 (regime.getArgs(), regime.getLogger(), regime.getTrainQueue(), regime.getValidQueue(), regime.getTrainFolderPath()),
                 dataPerGPU[gpu], nSamplesPerCopy[gpu], gpu, self.iterateOverSamples, self.replicaClass())
                for gpu in self.gpuIDs)
        return args

    @abstractmethod
    def initPathsList(self) -> list:
        raise NotImplementedError('subclasses must override initPathsList()!')

    @abstractmethod
    def initLossDictsList(self) -> list:
        raise NotImplementedError('subclasses must override initPathsList()!')

    @staticmethod
    @abstractmethod
    def iterateOverSamples(replica: Replica, lossFunc: callable, data: tuple, pathsHistoryDict: dict, pathsList: list, lossDictsList: list, gpu: int):
        raise NotImplementedError('subclasses must override iterateOverSamples()!')

    @abstractmethod
    def replicaClass(self) -> Replica:
        raise NotImplementedError('subclasses must override replicaClass()!')

    @staticmethod
    def evaluateSample(replica: Replica, lossFunc: callable, data: tuple, pathsHistoryDict: dict, pathsList: list, lossDictsList: list,
                       generateTrainParams: callable, addLossDict: callable):
        cModel = replica.getModel()
        input, target = data
        # select new path based on alphas distribution.
        # check that selected path hasn't been selected before
        pathWidthIdx = ModelReplicator._generateNewPath(replica, pathsHistoryDict)
        # add path to paths list
        pathsList.append([layer.widthRatioByIdx(p) for p, layer in zip(pathWidthIdx, cModel.layersList())])
        # train model on path
        # trainedPaths = replica.train(pathWidthIdx)
        trainParams = generateTrainParams(pathWidthIdx)
        trainedPaths = replica.train(trainParams)
        # switch to eval mode
        cModel.eval()
        # evaluate batch over trained paths
        with no_grad():
            for widthRatio, trainedPathIdx in trainedPaths.items():
                # set cModel path to trained path
                cModel.setCurrWidthIdx(trainedPathIdx)
                # forward input in model selected path
                logits = cModel(input)
                # calc loss
                lossDict = lossFunc(logits, target, cModel.countFlops())
                # add loss to container
                addLossDict(lossDict, lossDictsList, widthRatio, trainedPathIdx)

    @staticmethod
    def lossPerReplication(params):
        # extract transferred params to process
        buildModelFunc, lossFunc, modelStateDict, modelAlphas, pathsList, lossDictsList, trainWeightsElements, \
        data, nSamples, gpu, iterateOverSamples, replicaClass = params
        # set process GPU
        set_device(gpu)
        # init Replica instance on GPU with updated weights & alphas
        replica = replicaClass(buildModelFunc, modelStateDict, modelAlphas, gpu, trainWeightsElements)
        # init samples (paths) history, to make sure we don't select the same sample twice
        pathsHistoryDict = {}

        # iterate over samples. generate a sample (path), train it and evaluate alphas on sample
        for sampleIdx in range(nSamples):
            print('===== Sample idx:[{}/{}] - GPU:[{}] ====='.format(sampleIdx, nSamples, gpu))
            iterateOverSamples(replica, lossFunc, data, pathsHistoryDict, pathsList, lossDictsList, gpu)

        return lossDictsList, pathsList

# def lossPerReplication(self, args: tuple) -> (list, list):
#     # replica, data, nSamples = args
#     # input, target = data
#     # cModel = replica.cModel()
#     # gpu = replica.gpu()
#     regime, data, nSamples, gpu = args
#     input, target = data
#     # switch to process GPU
#     set_device(gpu)
#
#     # init replica with updated weights & alphas
#     replica = Replica(regime, self._modelStateDict, self._modelAlphas, gpu)
#     cModel = replica.cModel()
#
#     # init samples (paths) history, to make sure we don't select the same sample twice
#     pathsHistoryDict = {}
#     # init samples (paths) history list for logging purposes
#     pathsList = []
#
#     # init container to save loss values
#     lossDictsList = []
#
#     # iterate over samples. generate a sample (path), train it and evaluate alphas on sample
#     for sampleIdx in range(nSamples):
#         print('===== Sample idx:[{}/{}] - GPU:[{}] ====='.format(sampleIdx, nSamples, gpu))
#         # select new path based on alphas distribution.
#         # check that selected path hasn't been selected before
#         pathWidthIdx = self._generateNewPath(replica, cModel, pathsHistoryDict)
#         # add path to paths list
#         pathsList.append([layer.widthByIdx(p) for p, layer in zip(pathWidthIdx, cModel.layersList())])
#         # train model on path
#         trainedPaths = replica.train(pathWidthIdx)
#         # switch to eval mode
#         cModel.eval()
#         # evaluate batch over trained paths
#         with no_grad():
#             for trainedPathIdx in trainedPaths.values():
#                 # set cModel path to trained path
#                 cModel.setCurrWidthIdx(trainedPathIdx)
#                 # forward input in model selected path
#                 logits = cModel(input)
#                 # calc loss
#                 lossDict = self._flopsLoss(logits, target, cModel.countFlops())
#                 # add loss to container
#                 lossDictsList.append((lossDict, trainedPathIdx))
#
#     return lossDictsList, pathsList

# # calc loss distributed, i.e. for each model replication
# @abstractmethod
# def lossPerReplication(self, args):
#     raise NotImplementedError('subclasses must override lossPerReplication()!')

# # build args for pool.map
# @abstractmethod
# def buildArgs(self, inputPerGPU, targetPerGPU, nSamplesPerModel):
#     raise NotImplementedError('subclasses must override buildArgs()!')

# def demo(self, args):
#     (cModel, gpu), nSamples = args
#     # switch to process GPU
#     set_device(gpu)
#     assert (cModel.training is False)
#
#     data = randn(250, 3, 32, 32).cuda()
#
#     print('gpu [{}] start'.format(gpu))
#
#     with no_grad():
#         for _ in range(nSamples):
#             cModel(data)
#             data[0, 0, 0, 0] += 0.001
#
#     print('gpu [{}] end'.format(gpu))
#
# def replicationFunc(self, args):
#     self.demo(args)
#
# def run(self):
#     nCopies = len(self.replications)
#
#     nSamples = int(5000 / nCopies)
#     print('nSamples:[{}]'.format(nSamples))
#     args = ((r, nSamples) for r in self.replications)
#
#     with Pool(processes=nCopies, maxtasksperchild=1) as pool:
#         results = pool.map(self.replicationFunc, args)
#
#     return results

# # restore cModel original BNs
# def _restoreOriginalBNs(self):
#     for layer, (bn, widthList) in zip(self._cModel.layersList(), self._originalBNs):
#         layer.bn = bn
#         layer._widthList = widthList

# def _generatePathBNs(self, layerIdx: int):
#     model = self._cModel
#     # iterate over layers (except layerIdx) layer and generate new BNs
#     for idx, layer in enumerate(model.layersList()):
#         if idx == layerIdx:
#             continue
#
#         # get layer src BN based on currPathIdx
#         currBN = layer.bn[layer.currWidthIdx()]
#         bnFeatures = currBN.num_features
#         # generate layer new BNs ModuleList
#         newBNs = ModuleList([BatchNorm2d(bnFeatures) for _ in range(layer.nWidths())]).cuda()
#         # copy weights to new BNs
#         for bn in newBNs:
#             bn.load_state_dict(currBN.state_dict())
#         # set layer BNs
#         layer.bn = newBNs
#         # set layer width list to the same width
#         layer._widthList = [layer.currWidth()] * layer.nWidths()

# def jjj(self, args: tuple):
#     replica, data, nSamples = args
#
#     gpu = replica.gpu()
#     # switch to process GPU
#     set_device(gpu)
#
#     regime = replica._regime
#     model = regime.buildModel(regime.args)
#     model = model.cuda()
#     model.train()
#
#     from trainRegimes.PreTrainedRegime import PreTrainedTrainWeights
#     class YY(PreTrainedTrainWeights):
#         def __init__(self, regime, maxEpoch, model):
#             self._model = model
#             super(YY, self).__init__(regime, maxEpoch)
#
#         def getModel(self):
#             return self._model
#
#         def getModelParallel(self):
#             return self.getModel()
#
#         def widthList(self):
#             return {k: self.widthIdxList for k in range(4)}.items()
#
#     trainWeights = YY(regime, 5, model)
#     trainWeights.train('fiuiu')