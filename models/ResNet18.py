from abc import abstractmethod

from torch.nn import ModuleList, ReLU, Linear, AvgPool2d, MaxPool2d
from torch.nn.functional import linear

from models.BaseNet import BaseNetSwitcher
from models.modules.block import Block


class Input:
    def __init__(self, channels, output_size):
        self.channels = channels
        self.output_size = output_size

    # number of channels in input
    def currWidth(self):
        return self.channels

    def outputChannels(self):
        return self.channels

    def widthList(self):
        return [self.channels]

    def outputSize(self):
        return self.output_size


class Downsample(Block):
    def __init__(self, widthRatioList, in_planes, out_planes, stride1, prevLayer, conv2):
        super(Downsample, self).__init__()

        kernel_size = 1

        # init downsample source, i.e. in case we will need it
        convSlimLayer = BaseNetSwitcher.convSlimLayer()
        self.downsampleSrc = convSlimLayer(widthRatioList, in_planes, out_planes, kernel_size, stride1, prevLayer=prevLayer)
        # init current downsample
        self._downsample = [self.initCurrentDownsample()]
        # init residual function
        self.residualFunc = self.initResidual()

        # save conv2 layer reference
        self.conv2 = [conv2]

    @abstractmethod
    def initCurrentDownsample(self):
        raise NotImplementedError('subclasses must override initCurrentDownsample()!')

    @abstractmethod
    def initResidual(self):
        raise NotImplementedError('subclasses must override initResidual()!')

    def downsample(self):
        return self._downsample[0]

    def getOptimizationLayers(self):
        return []

    def getFlopsLayers(self):
        _downsample = self.downsample()
        return [] if _downsample is None else [_downsample]

    def getCountersLayers(self):
        return [self.downsampleSrc]

    def outputLayer(self):
        return self

    def residual(self, x):
        return self.residualFunc(x)

    # calc residual without downsample
    def standardResidual(self, x):
        return x

    # calc residual with downsample
    def downsampleResidual(self, x):
        _downsample = self.downsample()
        return _downsample(x)

    def generatePathBNs(self, srcLayer):
        self.downsampleSrc.generatePathBNs(srcLayer)


# downsample for block where downsample is always required, even for the same width
class PermanentDownsample(Downsample):
    def __init__(self, widthRatioList, in_planes, out_planes, stride1, prevLayer, conv2):
        super(PermanentDownsample, self).__init__(widthRatioList, in_planes, out_planes, stride1, prevLayer, conv2)

    def initCurrentDownsample(self):
        return self.downsampleSrc

    def initResidual(self):
        return self.downsampleResidual

    def updateCurrWidth(self):
        _downsample = self.downsample()
        _conv2 = self.conv2[0]
        # update downsample width
        _downsample.setCurrWidthIdx(_conv2.currWidthIdx())


# downsample for block where downsample is required only where following layers have different width
class TempDownsample(Downsample):
    def __init__(self, widthRatioList, in_planes, out_planes, stride1, prevLayer, conv2):
        super(TempDownsample, self).__init__(widthRatioList, in_planes, out_planes, stride1, prevLayer, conv2)

    def initCurrentDownsample(self):
        return None

    def initResidual(self):
        return self.standardResidual

    def updateCurrWidth(self):
        _prevLayer = self.downsampleSrc.prevLayer[0]
        _conv2 = self.conv2[0]
        # update downsampleSrc currWidthIdx according to conv2 currWidthIdx
        self.downsampleSrc.setCurrWidthIdx(_conv2.currWidthIdx())
        # set residual function to standard residual
        self.residualFunc = self.initResidual()
        # set downsample to None
        self._downsample = [self.initCurrentDownsample()]
        # check if widths are different, i.e. we need to use downsample
        prevWidth = _prevLayer.currWidth()
        conv2Width = _conv2.currWidth()

        if prevWidth != conv2Width:
            # update residual function
            self.residualFunc = self.downsampleResidual
            # update downsample
            self._downsample = [self.downsampleSrc]


class BasicBlock(Block):
    def __init__(self, widthRatioList, in_planes, out_planes, kernel_size, stride, prevLayer=None):
        super(BasicBlock, self).__init__()
        convSlimLayer = BaseNetSwitcher.convSlimLayer()

        stride1 = stride if in_planes == out_planes else (stride + 1)

        # build 1st block
        self.conv1 = convSlimLayer(widthRatioList, in_planes, out_planes, kernel_size, stride1, prevLayer=prevLayer)
        self.relu1 = ReLU(inplace=True)

        # build 2nd block
        self.conv2 = convSlimLayer(widthRatioList, out_planes, out_planes, kernel_size, stride, prevLayer=self.conv1)
        self.relu2 = ReLU(inplace=True)

        # init downsample
        downsampleClass = TempDownsample if in_planes == out_planes else PermanentDownsample
        self.downsample = downsampleClass(widthRatioList, in_planes, out_planes, stride1, prevLayer, self.conv2)

        # # register pre-forward hook
        # self.register_forward_pre_hook(self.preForward)

    # @staticmethod
    # def preForward(self, input):
    #     self.downsample.update()

    def forward(self, x):
        out = self.conv1(x)
        out = self.relu1(out)
        out = self.conv2(out)
        out += self.downsample.residual(x)
        out = self.relu2(out)

        return out

    def getOptimizationLayers(self):
        return [self.conv1, self.conv2]

    def getFlopsLayers(self):
        return [self.conv1] + self.downsample.getFlopsLayers() + [self.conv2]

    def getCountersLayers(self):
        return [self.conv1] + self.downsample.getCountersLayers() + [self.conv2]

    def outputLayer(self):
        return self.conv2

    def countFlops(self):
        return sum([layer.countFlops() for layer in self.getFlopsLayers()])

    def updateCurrWidth(self):
        self.downsample.updateCurrWidth()

    def generatePathBNs(self, srcLayer):
        self.conv1.generatePathBNs(srcLayer)
        if srcLayer != self.conv2:
            self.downsample.generatePathBNs(srcLayer)
            self.conv2.generatePathBNs(srcLayer)


class ResNet18(BaseNetSwitcher.getClass()):
    def __init__(self, args):
        super(ResNet18, self).__init__(args, initLayersParams=(args.width, args.nClasses, args.input_size, args.partition))

    # init layers (type, out_planes)
    def initBlocksPlanes(self):
        return [(self.convSlimLayer(), 16), (BasicBlock, 16), (BasicBlock, 16), (BasicBlock, 16),
                (BasicBlock, 32), (BasicBlock, 32), (BasicBlock, 32),
                (BasicBlock, 64), (BasicBlock, 64), (BasicBlock, 64)]

    @staticmethod
    def nPartitionBlocks():
        return 3, [4, 3, 3]

    @abstractmethod
    def initBlocks(self, params):
        raise NotImplementedError('subclasses must override initBlocks()!')

    @abstractmethod
    def forward(self, x):
        raise NotImplementedError('subclasses must override forward()!')

    # generate new BNs for current model path, except for given srcLayer
    def generatePathBNs(self, srcLayer: BaseNetSwitcher.convSlimLayer()):
        for block in self.blocks:
            block.generatePathBNs(srcLayer)

    # restore layers original BNs
    def restoreOriginalBNs(self):
        for layer in self._layers.forwardCounters():
            layer.restoreOriginalBNs()


class ResNet18_Cifar(ResNet18):
    def __init__(self, args):
        super(ResNet18_Cifar, self).__init__(args)

    def initBlocks(self, params):
        widthRatioList, nClasses, input_size, partition = params

        blocksPlanes = self.initBlocksPlanes()

        # init parameters
        kernel_size = 3
        stride = 1

        # create list of blocks from blocksPlanes
        blocks = ModuleList()
        prevLayer = Input(3, input_size)

        for i, (blockType, out_planes) in enumerate(blocksPlanes):
            layerWidthRatioList = widthRatioList.copy()
            # add partition ratio if exists
            if partition:
                layerWidthRatioList += [partition[i]]
            # build layer
            l = blockType(layerWidthRatioList, prevLayer.outputChannels(), out_planes, kernel_size, stride, prevLayer)
            # add layer to blocks list
            blocks.append(l)
            # update previous layer
            prevLayer = l.outputLayer()

        self.avgpool = AvgPool2d(8)
        self.fc = Linear(64, nClasses).cuda()

        return blocks

    def additionalLayersToLog(self):
        return [self.avgpool, self.fc]

    def forward(self, x):
        out = x
        for block in self.blocks:
            out = block(out)

        out = self.avgpool(out)
        out = out.view(out.size(0), -1)
        # narrow linear according to last conv2d layer
        in_features = int(self.fc.in_features * block.outputLayer().currWidthRatio())
        # out = linear(out, self.fc.weight.narrow(1, 0, block.outputLayer().currWidth()), bias=self.fc.bias)
        out = linear(out, self.fc.weight.narrow(1, 0, in_features), bias=self.fc.bias)

        return out


class ResNet18_Imagenet(ResNet18):
    def __init__(self, args):
        super(ResNet18_Imagenet, self).__init__(args)

    def initBlocks(self, params):
        widthRatioList, nClasses, input_size, partition = params

        blocksPlanes = self.initBlocksPlanes()

        # init parameters
        kernel_size = 7
        stride = 2

        # create list of blocks from blocksPlanes
        blocks = ModuleList()
        # output size is divided by 2 due to maxpool after 1st conv layer
        prevLayer = Input(3, int(input_size / 2))

        for i, (blockType, out_planes) in enumerate(blocksPlanes):
            # increase number of out_planes
            out_planes *= 4
            # copy width ratio list
            layerWidthRatioList = widthRatioList.copy()
            # add partition ratio if exists
            if partition:
                layerWidthRatioList += [partition[i]]
            # build layer
            l = blockType(layerWidthRatioList, prevLayer.outputChannels(), out_planes, kernel_size, stride, prevLayer)
            # update kernel size
            kernel_size = 3
            # update stride
            stride = 1
            # add layer to blocks list
            blocks.append(l)
            # update previous layer
            prevLayer = l.outputLayer()

        self.maxpool = MaxPool2d(kernel_size=kernel_size, stride=2, padding=1)
        self.avgpool = AvgPool2d(7)
        self.fc = Linear(1024, nClasses).cuda()

        return blocks

    def additionalLayersToLog(self):
        return [self.maxpool, self.avgpool, self.fc]

    def forward(self, x):
        out = x

        block = self.blocks[0]
        out = block(out)
        out = self.maxpool(out)

        for block in self.blocks[1:]:
            out = block(out)

        out = self.avgpool(out)
        out = out.view(out.size(0), -1)
        # narrow linear according to last conv2d layer
        in_features = int(self.fc.in_features * block.outputLayer().currWidthRatio())
        out = linear(out, self.fc.weight.narrow(1, 0, in_features), bias=self.fc.bias)

        return out
