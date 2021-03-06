from abc import abstractmethod
from os.path import exists

from torch.nn import Module

from utils.HtmlLogger import HtmlLogger


class BaseNet(Module):
    class Layers:
        def __init__(self, blocks):
            self._blocks = blocks
            # assuming these lists layers don't change
            self._optim = self._buildLayersList(blocks, lambda block: block.getOptimizationLayers())
            self._forwardCounters = self._buildLayersList(blocks, lambda block: block.getCountersLayers())

        def _buildLayersList(self, blocks, getLayersFunc):
            layersList = []
            for block in blocks:
                layersList.extend(getLayersFunc(block))

            return layersList

        def optimization(self):
            return self._optim

        def flops(self):
            return self._buildLayersList(self._blocks, lambda block: block.getFlopsLayers())

        def forwardCounters(self):
            return self._forwardCounters

    _modelFlopsKey = 'modelFlops'
    _partitionKey = 'Partition'
    _baselineFlopsKey = 'baselineFlops'
    _baselineFlopsRatioKey = 'baselineFlopsRatio'
    _alphasDistributionKey = 'Alphas distribution'
    # init args dict we have to sort by their values
    _keysToSortByValue = [_baselineFlopsRatioKey, _baselineFlopsKey]

    def __init__(self, args, initLayersParams):
        super(BaseNet, self).__init__()
        # init save folder
        saveFolder = args.save
        # init count flops flag
        modelFlops = getattr(args, self._modelFlopsKey)
        countFlopsFlag = modelFlops is None
        # init layers
        self.blocks = self.initBlocks(initLayersParams, countFlopsFlag)
        # init Layers class instance
        self._layers = self.Layers(self.blocks)
        # init model alphas
        self._alphas = self._initAlphas(saveFolder)

        # save model current random weights
        self._randomWeights = None
        if args.saveRandomWeights:
            self._randomWeights = {k: v.clone() for k, v in self.state_dict().items()}
            args.saveRandomWeights = False

        if not countFlopsFlag:
            # set layers flops data from args.modelFlops
            self._setLayersFlopsData(modelFlops)
        else:
            # build args.modelFlops from layers flops data
            setattr(args, self._modelFlopsKey, self._getLayersFlopsData())

        # build dictionary of layer width indices list per width ratio
        self._baselineWidth, self.baselineFlops = self._buildBaselineWidthDict(args)
        # print model to file
        self.printToFile(saveFolder)
        # # calc number of width permutations in model
        # self.nPerms = reduce(lambda x, y: x * y, [layer.nWidths() for layer in self._layers.optimization()])

    @abstractmethod
    def initBlocks(self, params, countFlopsFlag):
        raise NotImplementedError('subclasses must override initLayers()!')

    @staticmethod
    @abstractmethod
    # number of model blocks for partition, in order to generate different width for each block
    # returns tuple (number of blocks as int, list of number of layer in each block)
    def nPartitionBlocks():
        raise NotImplementedError('subclasses must override nPartitionBlocks()!')

    @abstractmethod
    def forward(self, x):
        raise NotImplementedError('subclasses must override forward()!')

    # choose alpha based on alphas distribution
    @abstractmethod
    def choosePathByAlphas(self):
        raise NotImplementedError('subclasses must override choosePathByAlphas()!')

    # choose path based on alphas, without drawing from the distribution
    @abstractmethod
    def choosePathAlphasAsPartition(self):
        raise NotImplementedError('subclasses must override choosePathByAlphas()!')

    # restore model original state_dict structure
    @abstractmethod
    def restoreOriginalStateDictStructure(self):
        raise NotImplementedError('subclasses must override restoreOriginalState()!')

    @abstractmethod
    def _alphasClass(self):
        raise NotImplementedError('subclasses must override alphasClass()!')

    def _initAlphas(self, saveFolder: str):
        _alphasClass = self._alphasClass()
        return _alphasClass(self, saveFolder)

    @staticmethod
    def modelFlopsKey():
        return BaseNet._modelFlopsKey

    @staticmethod
    def modelFlopsPathKey():
        return '{}_Path'.format(BaseNet.modelFlopsKey())

    @staticmethod
    def partitionKey():
        return BaseNet._partitionKey

    @staticmethod
    def baselineFlopsKey():
        return BaseNet._baselineFlopsKey

    @staticmethod
    def alphasDistributionKey():
        return BaseNet._alphasDistributionKey

    @staticmethod
    def keysToSortByValue():
        return BaseNet._keysToSortByValue

    def additionalLayersToLog(self):
        return []

    def _setLayersFlopsData(self, _modelFlops):
        for layer, layerFlopsData in zip(self._layers.forwardCounters(), _modelFlops):
            layer.setFlopsData(layerFlopsData)

    def _getLayersFlopsData(self):
        return [layer.getFlopsData() for layer in self._layers.forwardCounters()]

    def loadRandomWeights(self, logger=None):
        self.load_state_dict(self._randomWeights)
        if logger:
            logger.addInfoTable('Loaded random weights', [[True]])

    def countFlops(self):
        return sum([block.countFlops() for block in self.blocks])

    def flopsRatio(self):
        return self.countFlops() / self.baselineFlops

    def layersList(self):
        return self._layers.optimization()

    def nLayers(self):
        return len(self.layersList())

    def alphas(self) -> list:
        return self._alphas.alphas()

    def updateAlphas(self, srcModelAlphas):
        self._alphas.update(srcModelAlphas)

    # adds partition flops if partition exists
    def _addPartitionFlops(self, args):
        # get baseline flops from args
        argsBaselineFlops = getattr(args, self._baselineFlopsKey, None)

        if args.partition:
            # add partition width to layers in 1st position in layers' widthList & widthRatioList
            assert (len(self.layersList()) == len(args.partition))
            for layer, p in zip(self.layersList(), args.partition):
                layer.addWidth(p)
            # add extra width to non-optimization layers
            nWidthNew = layer.nWidths()
            for layer in self._layers.forwardCounters():
                nWidthLayer = layer.nWidths()
                if nWidthLayer < nWidthNew:
                    assert ((nWidthNew - nWidthLayer) == 1)
                    layer.addWidth(0.0)

            # init partition path indices
            partitionPathIndices = [(layer.nWidths() - 1) for layer in self.layersList()]
            # make sure partition has been set correctly
            assert (len(args.partition) == len(partitionPathIndices) == len(self.layersList()))
            for p, idx, layer in zip(args.partition, partitionPathIndices, self.layersList()):
                assert (layer.widthRatioByIdx(idx) == p)
            # add partition path indices to baseline
            self._baselineWidth = {self._partitionKey: partitionPathIndices}
            # calc partition path flops
            # we use calcBaselineFlops() because there is only the partition path in self._baselineWidth
            argsBaselineFlops.update(self.calcBaselineFlops(restoreOrgStateFlag=False))
            # add partition flops to args.baselineFlops
            setattr(args, self._baselineFlopsKey, argsBaselineFlops)
            # add partition path flops ratio to args
            argsFlopsRatio = getattr(args, self._baselineFlopsRatioKey)
            argsFlopsRatio[self._partitionKey] = argsBaselineFlops[self._partitionKey] / argsBaselineFlops[args.baseline]
            # add baseline models widths flops ratio to args
            setattr(args, self._baselineFlopsRatioKey, argsFlopsRatio)

        return argsBaselineFlops

    def _buildBaselineWidthDict(self, args):
        # add partition flops if partition exists
        argsBaselineFlops = self._addPartitionFlops(args)

        # init baseline width with homogeneous paths indices
        _baselineWidth = self.buildHomogeneousWidthIdx(args.width)
        # add partition path indices to baseline width if partition exists
        if args.partition:
            _baselineWidth.update(self._baselineWidth)

        # calc baseline flops if args baseline flops is None
        if argsBaselineFlops is None:
            # set self._baselineWidth for calcBaselineFlops()
            self._baselineWidth = _baselineWidth
            # calc baseline flops
            argsBaselineFlops = self.calcBaselineFlops(restoreOrgStateFlag=True)
            # update args baseline flops & flops ratio
            setattr(args, self._baselineFlopsKey, argsBaselineFlops)
            _baselineFlops = argsBaselineFlops[args.baseline]
            setattr(args, self._baselineFlopsRatioKey, {k: (v / _baselineFlops) for k, v in argsBaselineFlops.items()})

        return _baselineWidth, argsBaselineFlops[args.baseline]

    def baselineWidthKeys(self):
        return list(self._baselineWidth.keys())

    def baselineWidth(self):
        for v in self._baselineWidth.items():
            yield v

    def currWidth(self):
        return [layer.currWidth() for layer in self._layers.optimization()]

    def currWidthIdx(self):
        return [layer.currWidthIdx() for layer in self._layers.optimization()]

    def currWidthRatio(self):
        return [layer.currWidthRatio() for layer in self._layers.optimization()]

    def setCurrWidthIdx(self, idxList: list):
        assert (len(self._layers.optimization()) == len(idxList))
        for layer, idx in zip(self._layers.optimization(), idxList):
            layer.setCurrWidthIdx(idx)

        # update current width changes in each block
        for block in self.blocks:
            block.updateCurrWidth()

    # build a dictionary where each key is width ratio and each value is the list of layer indices in order to set the key width ratio as current
    # width in each layer
    def buildHomogeneousWidthIdx(self, widthRatioList):
        homogeneousWidth = {}

        for widthRatio in widthRatioList:
            # check if width is not in baselineResults dictionary
            if widthRatio not in homogeneousWidth:
                # build layer indices for current width ratio
                homogeneousWidth[widthRatio] = [l.widthRatioIdx(widthRatio) for l in self._layers.optimization()]

        return homogeneousWidth

    def calcBaselineFlops(self, restoreOrgStateFlag):
        return self.applyOnBaseline(self.countFlops, restoreOrgStateFlag)

    # apply some function on baseline model
    # baseline model are per layer width
    # this function create a map from baseline width to func() result on baseline model
    # def applyOnBaseline(self, func, applyOnAlphasDistribution=False):
    def applyOnBaseline(self, func, restoreOrgStateFlag):
        baselineResults = {}
        # save current model width indices
        modelCurrWidthIdx = self.currWidthIdx()
        # iterate over width ratios
        for widthRatio, idxList in self.baselineWidth():
            # set model layers current width index
            self.setCurrWidthIdx(idxList)
            # update value in dictionary
            baselineResults[widthRatio] = func()

        # # apply on current alphas distribution
        # if applyOnAlphasDistribution:
        #     self.setFiltersByAlphas()
        #     # &#945; is greek alpha symbol in HTML
        #     baselineResults['&#945;'] = func()

        # restore model layers current width
        self.setCurrWidthIdx(modelCurrWidthIdx)
        # restore model original state
        if restoreOrgStateFlag:
            self.restoreOriginalStateDictStructure()

        return baselineResults

    def loadPreTrained(self, state_dict):
        self.load_state_dict(state_dict)

    def saveAlphasCsv(self, data: list):
        self._alphas.saveCsv(self, data)

    def logTopAlphas(self, k, loggerFuncs, logLayer=False):
        return self._alphas.logTopAlphas(self, k, loggerFuncs, logLayer)

    def printToFile(self, saveFolder):
        fileName = 'model'

        logger = HtmlLogger(saveFolder, fileName)
        if exists(logger.fullPath):
            return

        logger.setMaxTableCellLength(1000)

        layerIdxKey = 'Layer#'
        nFiltersKey = 'Filters#'
        widthsKey = 'Width'
        layerArchKey = 'Layer Architecture'

        logger.createDataTable('Model architecture', [layerIdxKey, nFiltersKey, widthsKey, layerArchKey])
        for layerIdx, layer in enumerate(self._layers.flops()):
            widths = layer.widthList()

            dataRow = {layerIdxKey: layerIdx, nFiltersKey: layer.outputChannels(), widthsKey: [widths], layerArchKey: layer}
            logger.addDataRow(dataRow)

        layerIdx += 1
        # log additional layers, like Linear, MaxPool2d, AvgPool2d
        for layer in self.additionalLayersToLog():
            dataRow = {layerIdxKey: layerIdx, layerArchKey: layer}
            logger.addDataRow(dataRow)
            layerIdx += 1

        # log layers alphas distribution
        self.logTopAlphas(len(widths), loggerFuncs=[lambda k, rows: logger.addInfoTable(self._alphasDistributionKey, rows)], logLayer=True)
        # reset table max cell length
        logger.resetMaxTableCellLength()

    def _resetForwardCounters(self):
        for layer in self._layers.forwardCounters():
            # reset layer forward counters
            layer.resetForwardCounters()

    def logForwardCounters(self, loggerFuncs):
        if isinstance(loggerFuncs, list) and len(loggerFuncs) > 0:
            rows = [['Layer #', 'Layer', 'Counters']]
            counterCols = ['Width', 'Counter']

            for layerIdx, layer in enumerate(self._layers.forwardCounters()):
                layerForwardCounters = layer.forwardCounters()
                # build layer data row
                layerRows = [counterCols]
                # add layer forward counters in descending order to table rows
                for width, counter in sorted(layerForwardCounters.items(), key=lambda kv: (kv[-1], kv[0]), reverse=True):
                    layerRows.append([width, counter])
                # add summary row
                layerRows.append(['Total', sum(layerForwardCounters.values())])

                # add layer row to model table
                rows.append([layerIdx, layer, layerRows])

            # apply loggers functions
            for f in loggerFuncs:
                f(rows)

        # reset counters
        self._resetForwardCounters()

# flops = []
# for layer in self._layers.forwardCounters():
#     flops.append((layer.flopsDict, layer.output_size))
#
# from torch import save
# save(flops, 'flops.pth.tar')

# def loadPreTrained(self, state_dict):
#     from collections import OrderedDict
#     newDict = OrderedDict()
#
#     tokenOrg = '.downsample.'
#     tokenNew = '.downsample.downsampleSrc.'
#     for key in state_dict.keys():
#         if tokenOrg in key:
#             newDict[key.replace(tokenOrg, tokenNew)] = state_dict[key]
#         else:
#             newDict[key] = state_dict[key]
#
#     currDict = self.state_dict()
#     for key in currDict.keys():
#         if key not in newDict:
#             newDict[key] = currDict[key]
#
#     # load weights
#     self.load_state_dict(newDict)

# def loadPreTrained(self, path, logger):
#     loggerRows = []
#     if path is not None:
#         if exists(path):
#             # load checkpoint
#             checkpoint = loadModel(path, map_location=lambda storage, loc: storage.cuda())
#             # set checkpoint state dict
#             chckpntStateDict = checkpoint['state_dict']
#             # load model state dict keys
#             modelStateDict = self.state_dict()
#             modelStateDictKeys = set(modelStateDict.keys())
#             # compare dictionaries
#             dictDiff = modelStateDictKeys.symmetric_difference(set(chckpntStateDict.keys()))
#
#             # init batchnorm token
#             token = '.bn.'
#             # init how many batchnorms we have loaded from pre-trained
#             bnLoadedCounter = 0
#             # init how many batchnorms are in total
#             bnTotalCounter = sum(1 for key in modelStateDictKeys if token in key)
#
#             # init new dict, based on current dict
#             newDict = modelStateDict
#             # iterate over checkpoint state dict keys
#             for key in chckpntStateDict.keys():
#                 # duplicate values with their corresponding new keys
#                 if token in key:
#                     # filters out num_batches_tracked in cases it is not needed
#                     if key in modelStateDict:
#                         if modelStateDict[key].size() == chckpntStateDict[key].size():
#                             newDict[key] = chckpntStateDict[key]
#                             bnLoadedCounter += 1
#                         else:
#                             # add model state dict values to new dict
#                             newDict[key] = modelStateDict[key]
#                 else:
#                     # add checkpoint state dict values to new dict
#                     newDict[key] = chckpntStateDict[key]
#
#             # load weights
#             self.load_state_dict(newDict)
#             # add info rows about checkpoint
#             loggerRows.append(['Path', '{}'.format(path)])
#             validationAccRows = [['Ratio', 'Accuracy']] + HtmlLogger.dictToRows(checkpoint['best_prec1'], nElementPerRow=1)
#             loggerRows.append(['Validation accuracy', validationAccRows])
#             loggerRows.append(['StateDict diff', list(dictDiff)])
#             loggerRows.append(['Loaded Batchnorm #', '{}/{}'.format(bnLoadedCounter, bnTotalCounter)])
#         else:
#             raise ValueError('Failed to load pre-trained from [{}], path does not exists'.format(path))
#
#         # load pre-trained model if we tried to load pre-trained
#         logger.addInfoTable('Pre-trained model', loggerRows)


# def loadPreTrained(self, path, logger):
# # replace old key (.layers.) with new key (.blocks.)
# chckpntUpdatedDict = OrderedDict()
# oldKey = 'layers.'
# newKey = 'blocks.'
# for dictKey in chckpntStateDict.keys():
#     newDictKey = dictKey
#     if oldKey in dictKey:
#         newDictKey = dictKey.replace(oldKey, newKey)
#
#     chckpntUpdatedDict[newDictKey] = chckpntStateDict[dictKey]

# # duplicate BN weights
# if duplicateBNWeights:
#     # init new dict, since we have to add new keys
#     newDict = OrderedDict()
#     # init tokens
#     token = '.bn.0.'
#     template = '.bn.{}.'
#     # iterate over checkpoint state dict keys
#     for key in chckpntStateDict.keys():
#         # add checkpoint state dict values to new dict
#         newDict[key] = chckpntStateDict[key]
#         # duplicate values with their corresponding new keys
#         if token in key:
#             idx = 1
#             newKey = key.replace(token, template.format(idx))
#             while newKey in dictDiff:
#                 newDict[newKey] = chckpntStateDict[key]
#                 dictDiff.remove(newKey)
#                 idx += 1
#                 newKey = key.replace(token, template.format(idx))
#
#     # update the state dict we want to load to model
#     chckpntStateDict = newDict
