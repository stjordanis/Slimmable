from abc import abstractmethod
from time import time
from os import makedirs
from os.path import exists

from torch import tensor, no_grad
from torch.nn.parallel.data_parallel import DataParallel
from torch.nn import CrossEntropyLoss
from torch.optim.sgd import SGD
from torch.optim.lr_scheduler import ReduceLROnPlateau

import models
from utils.data import load_data
from utils.args import logParameters
from utils.checkpoint import save_checkpoint
from utils.HtmlLogger import HtmlLogger
from utils.training import TrainingStats, TrainingOptimum
from utils.statistics import Statistics


class TrainRegime:
    trainLossKey = 'Training loss'
    trainAccKey = 'Training acc'
    validLossKey = 'Validation loss'
    validAccKey = 'Validation acc'
    archLossKey = 'Arch loss'
    crossEntropyKey = 'CrossEntropy loss'
    flopsLossKey = 'Flops loss'
    epochNumKey = 'Epoch #'
    batchNumKey = 'Batch #'
    pathFlopsRatioKey = 'Path flops ratio'
    validFlopsRatioKey = 'Validation flops ratio'
    timeKey = 'Time'
    lrKey = 'Optimizer lr'
    widthKey = 'Width'
    forwardCountersKey = 'Forward counters'

    # init formats for keys
    formats = {
        timeKey: lambda x: '{:.3f}'.format(x),
        lrKey: lambda x: '{:.8f}'.format(x),
        archLossKey: lambda x: '{:.5f}'.format(x),
        crossEntropyKey: lambda x: '{:.5f}'.format(x),
        flopsLossKey: lambda x: '{:.5f}'.format(x),
        trainLossKey: lambda x: HtmlLogger.dictToRows(x, nElementPerRow=1),
        trainAccKey: lambda x: HtmlLogger.dictToRows(x, nElementPerRow=1),
        validLossKey: lambda x: HtmlLogger.dictToRows(x, nElementPerRow=1),
        validAccKey: lambda x: HtmlLogger.dictToRows(x, nElementPerRow=1),
        pathFlopsRatioKey: lambda x: '{:.3f}'.format(x),
        validFlopsRatioKey: lambda x: '{:.3f}'.format(x)
    }

    initWeightsTrainTableTitle = 'Initial weights training'
    k = 2
    alphasTableTitle = 'Alphas (top [{}])'.format(k)

    colsMainLogger = [epochNumKey, archLossKey, trainLossKey, trainAccKey, validLossKey, validAccKey, validFlopsRatioKey, widthKey, lrKey]
    colsMainInitWeightsTrain = [epochNumKey, trainLossKey, trainAccKey, validLossKey, validAccKey, lrKey]
    colsTrainWeights = [batchNumKey, trainLossKey, trainAccKey, timeKey]
    colsValidation = [batchNumKey, validLossKey, validAccKey, timeKey]
    colsTrainAlphas = [batchNumKey, archLossKey, crossEntropyKey, flopsLossKey, alphasTableTitle, forwardCountersKey, timeKey]

    def __init__(self, args, logger):
        # init model
        model = self.buildModel(args)
        model = model.cuda()
        # create DataParallel model instance
        # self.modelParallel = model
        self.modelParallel = DataParallel(model, args.gpu)
        assert (id(model) == id(self.modelParallel.module))

        # load data
        self.train_queue, self.search_queue, self.valid_queue = load_data(args)
        # load pre-trained model
        model.loadPreTrained(args.pre_trained, logger)

        # # init statistics instance
        # self.statistics = Statistics(args.save)

        # log parameters
        logParameters(logger, args, model)

        self.args = args
        self.model = model
        self.logger = logger

        self.trainFolderPath = '{}/{}'.format(args.save, args.trainFolder)

        # init cross entropy loss
        self.cross_entropy = CrossEntropyLoss().cuda()

    @abstractmethod
    def train(self):
        raise NotImplementedError('subclasses must override train()!')

    def buildModel(self, args):
        # get model constructor
        modelKey = '{}_{}'.format(args.model, args.dataset)
        modelClass = models.__dict__[modelKey]

        return modelClass(args)

    # apply defined format functions on dict values by keys
    def _applyFormats(self, dict):
        for k in dict.keys():
            if k in self.formats:
                dict[k] = self.formats[k](dict[k])

    def trainWeights(self, optimizer, epoch, loggers):
        print('*** trainWeights() ***')
        model = self.model
        modelParallel = self.modelParallel
        crit = self.cross_entropy
        train_queue = self.train_queue
        trainStats = TrainingStats(model.baselineWidthKeys())

        trainLogger = loggers.get('train')
        if trainLogger:
            trainLogger.createDataTable('Epoch:[{}] - Training weights'.format(epoch), self.colsTrainWeights)

        nBatches = len(train_queue)

        modelParallel.train()
        assert (model.training is True)

        for step, (input, target) in enumerate(train_queue):
            startTime = time()

            input = tensor(input, requires_grad=False).cuda()
            target = tensor(target, requires_grad=False).cuda(async=True)

            # optimize model weights
            optimizer.zero_grad()
            # iterate & forward widths
            for widthRatio, idxList in model.baselineWidth():
                # set model layers current width index
                model.setCurrWidthIdx(idxList)
                # forward
                logits = modelParallel(input)
                # calc loss
                loss = crit(logits, target)
                # back propagate
                loss.backward()
                # update training stats
                trainStats.update(widthRatio, logits, target, loss)
            # update weights
            optimizer.step()

            endTime = time()

            if trainLogger:
                dataRow = {
                    self.batchNumKey: '{}/{}'.format(step, nBatches), self.timeKey: (endTime - startTime),
                    self.trainLossKey: trainStats.batchLoss(), self.trainAccKey: trainStats.prec1()
                }
                # apply formats
                self._applyFormats(dataRow)
                # add row to data table
                trainLogger.addDataRow(dataRow)

        epochLossDict = trainStats.epochLoss()
        epochAccDict = trainStats.top1()
        # # add epoch data to statistics plots
        # self.statistics.addBatchData(epochLossDict, epochAccDict)
        # log accuracy, loss, etc.
        summaryData = {self.trainLossKey: epochLossDict, self.trainAccKey: epochAccDict, self.batchNumKey: 'Summary'}
        # apply formats
        self._applyFormats(summaryData)

        for logger in loggers.values():
            if logger:
                logger.addSummaryDataRow(summaryData)

        # log forward counters. if loggerFuncs==[] then it is just resets counters
        func = [lambda rows: trainLogger.addInfoTable(title='{} - Training'.format(self.forwardCountersKey), rows=rows)] if trainLogger else []
        model.logForwardCounters(loggerFuncs=func)

        return summaryData

    def infer(self, nEpoch, loggers):
        print('*** infer() ***')
        model = self.model
        modelParallel = self.modelParallel
        valid_queue = self.valid_queue
        crit = self.cross_entropy
        trainStats = TrainingStats(model.baselineWidthKeys())

        trainLogger = loggers.get('train')
        if trainLogger:
            trainLogger.createDataTable('Epoch:[{}] - Validation'.format(nEpoch), self.colsValidation)

        nBatches = len(valid_queue)

        modelParallel.eval()
        assert (model.training is False)

        with no_grad():
            for step, (input, target) in enumerate(valid_queue):
                startTime = time()

                input = tensor(input).cuda()
                target = tensor(target).cuda(async=True)

                # iterate & forward widths
                for widthRatio, idxList in model.baselineWidth():
                    # set model layers current width index
                    model.setCurrWidthIdx(idxList)
                    # forward
                    logits = modelParallel(input)
                    # calc loss
                    loss = crit(logits, target)
                    # update training stats
                    trainStats.update(widthRatio, logits, target, loss)

                endTime = time()

                if trainLogger:
                    dataRow = {
                        self.batchNumKey: '{}/{}'.format(step, nBatches), self.validLossKey: trainStats.batchLoss(),
                        self.validAccKey: trainStats.prec1(), self.timeKey: endTime - startTime
                    }
                    # apply formats
                    self._applyFormats(dataRow)
                    # add row to data table
                    trainLogger.addDataRow(dataRow)

        # create summary row
        validAcc = trainStats.top1()
        validLoss = trainStats.epochLoss()
        summaryRow = {self.batchNumKey: 'Summary', self.validLossKey: validLoss, self.validAccKey: validAcc}
        # apply formats
        self._applyFormats(summaryRow)

        for logger in loggers.values():
            if logger:
                logger.addSummaryDataRow(summaryRow)

        # log forward counters. if loggerFuncs==[] then it is just resets counters
        func = [lambda rows: trainLogger.addInfoTable(title='{} - Validation'.format(self.forwardCountersKey), rows=rows)] if trainLogger else []
        model.logForwardCounters(loggerFuncs=func)

        return validAcc, validLoss, summaryRow

    def initialWeightsTraining(self, trainFolderName, filename=None):
        model = self.model
        modelParallel = self.modelParallel
        args = self.args
        logger = self.logger

        # create train folder
        folderPath = '{}/{}'.format(self.trainFolderPath, trainFolderName)
        if not exists(folderPath):
            makedirs(folderPath)

        # init optimizer
        optimizer = SGD(modelParallel.parameters(), args.learning_rate, momentum=args.momentum, weight_decay=args.weight_decay)

        # init table in main logger
        logger.createDataTable(self.initWeightsTrainTableTitle, self.colsMainInitWeightsTrain)

        # # calc alpha trainset loss on baselines
        # self.calcAlphaTrainsetLossOnBaselines(folderPath, self.archLossKey, logger)

        # init optimum info table headers
        optimumTableHeaders = [self.widthKey, self.validAccKey, self.epochNumKey, 'Epochs as optimum']
        # init TrainingOptimum instance
        trainOptimum = TrainingOptimum(model.baselineWidthKeys(), optimumTableHeaders, lambda value, optValue: value > optValue)
        # init optimal epoch data, we will display it in summary row
        optimalEpochData = None

        # count how many epochs current optimum hasn't changed
        epoch = 0
        nEpochsOptimum = 0
        trainLoggerFlag = True

        # init scheduler
        scheduler = ReduceLROnPlateau(optimizer, mode='min', factor=0.95, patience=2, min_lr=args.learning_rate_min)

        while nEpochsOptimum <= args.optimal_epochs:
            # update epoch number
            epoch += 1
            # init train logger
            trainLogger = None
            if trainLoggerFlag:
                trainLogger = HtmlLogger(folderPath, str(epoch))
                trainLogger.addInfoTable('Learning rates', [['optimizer_lr', self.formats[self.lrKey](optimizer.param_groups[0]['lr'])]])

            # update train logger condition for next epoch
            trainLoggerFlag = ((epoch + 1) % args.logInterval) == 0

            # set loggers dictionary
            loggersDict = dict(train=trainLogger)
            # training
            print('========== Epoch:[{}] =============='.format(epoch))
            trainData = self.trainWeights(optimizer, epoch, loggersDict)

            # add epoch number
            trainData[self.epochNumKey] = epoch
            # add learning rate
            trainData[self.lrKey] = self.formats[self.lrKey](optimizer.param_groups[0]['lr'])

            # validation
            validAcc, validLoss, validData = self.infer(epoch, loggersDict)
            # merge trainData with validData
            for k, v in validData.items():
                trainData[k] = v

            # update scheduler
            scheduler.step(trainOptimum.dictAvg(validLoss))

            # update optimum values according to current epoch values and get optimum table for logger
            optimumTable = trainOptimum.update(validAcc, epoch)
            # add update time to optimum table
            optimumTable.append(['Update time', logger.getTimeStr()])
            # update nEpochsOptimum table
            logger.addInfoTable('Optimum', optimumTable)

            # update best precision only after switching stage is complete
            is_best = trainOptimum.is_best(epoch)
            if is_best:
                # update optimal epoch data
                optimalEpochData = (validAcc, validLoss)
                # found new optimum, reset nEpochsOptimum
                nEpochsOptimum = 0
            else:
                # optimum hasn't changed
                nEpochsOptimum += 1

            # save model checkpoint
            save_checkpoint(self.trainFolderPath, model, optimizer, validAcc, is_best, filename)

            # add data to main logger table
            logger.addDataRow(trainData)

        # add optimal accuracy
        optAcc, optLoss = optimalEpochData
        summaryRow = {self.epochNumKey: 'Optimal', self.validAccKey: optAcc, self.validLossKey: optLoss}
        self._applyFormats(summaryRow)
        logger.addSummaryDataRow(summaryRow)

        # # save pre-trained checkpoint
        # save_checkpoint(self.trainFolderPath, model, args, epoch, best_prec1, is_best=False, filename='pre_trained')

        # save optimal validation values
        setattr(args, self.validAccKey, optAcc)
        setattr(args, self.validLossKey, optLoss)
