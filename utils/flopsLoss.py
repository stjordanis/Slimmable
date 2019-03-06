from numpy import linspace
from bisect import bisect_left

from torch import tensor, float32, sigmoid
from torch.nn import CrossEntropyLoss, Module, LeakyReLU
from torch.serialization import load
import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages


class LossDiff:
    def __init__(self):
        self._lossFunc = LeakyReLU(0.1)

    def calcLoss(self, x: tensor) -> tensor:
        return self._lossFunc.negative_slope + self._lossFunc(x)


# loss function class
class LossFunction:
    def __init__(self, minFlops: float):
        self.minFlops = minFlops

    def calcLoss(self, modelFlops: float) -> tensor:
        v = (modelFlops / self.minFlops) ** 2
        return tensor(v, dtype=float32).cuda()


class FlopsLoss(Module):
    # init loss types keys
    _totalKey = 'Total'
    _crossEntropyKey = 'CrossEntropy'
    _flopsKey = 'Flops'
    _lossKeys = [_totalKey, _crossEntropyKey, _flopsKey]

    def __init__(self, args, baselineFlopsDict: dict):
        super(FlopsLoss, self).__init__()

        self.lmbda = args.lmbda
        self.crossEntropyLoss = CrossEntropyLoss().cuda()
        self.baselineFlops = baselineFlopsDict.get(args.baseline)

        # self.flopsLoss = LossFunction(self.baselineFlops).calcLoss
        # self.flopsLossImgPath = '{}/flops_loss_func.pdf'.format(args.save)
        # self._plotFunction(self.flopsLoss, baselineFlopsDict.values())

        homogeneousLoss = load('homogeneous.pth.tar')
        self._linearLineParams = homogeneousLoss.linearLineParams
        self._flopsList = sorted(homogeneousLoss.flopsDict.keys())

        self._flopsLoss = LossDiff().calcLoss
        self.flopsLossImgPath = '{}/flops_loss_func.pdf'.format(args.save)
        self._plotFunction(lambda x: self._flopsLoss(tensor(x)), [-2., -1., -0.1, -0.05, 0., 0.05, 0.1, 1., 2.])

    @staticmethod
    def lossKeys() -> str:
        return FlopsLoss._lossKeys

    @staticmethod
    def totalKey() -> str:
        return FlopsLoss._totalKey

    # def forward(self, input: tensor, target: tensor, modelFlops: float) -> dict:
    #     loss = {self._crossEntropyKey: self.crossEntropyLoss(input, target),
    #             self._flopsKey: self.lmbda * self.flopsLoss(modelFlops)}
    #     loss[self._totalKey] = sum(loss.values())
    #
    #     return loss

    def forward(self, input: tensor, target: tensor, modelFlops: float) -> dict:
        loss = {self._crossEntropyKey: self.crossEntropyLoss(input, target),
                self._flopsKey: tensor(modelFlops, dtype=float32).cuda()}

        # find modelFlops corresponding linear line
        flopsIdx = bisect_left(self._flopsList, modelFlops)
        if flopsIdx <= 0:
            # it is possible to select configuration with flops less than homogeneous 0.25
            x0, x1 = self._flopsList[0:2]
        else:
            x0, x1 = self._flopsList[flopsIdx - 1:flopsIdx + 1]
            assert (x0 <= modelFlops <= x1)
        m, b = self._linearLineParams[(x0, x1)]
        # calc expected loss for modelFlops
        expectedLoss = (m * modelFlops) + b
        lossDiff = loss[self._crossEntropyKey] - expectedLoss
        loss[self._totalKey] = self._flopsLoss(lossDiff / expectedLoss)

        return loss

    def _plotFunction(self, func, xRange):
        xMin, xMax = min(xRange), max(xRange)
        # build data for function
        nPts = (5 * 100) + 1
        ptsGap = int((nPts - 1) / 20)

        pts = linspace(xMin, xMax, nPts).tolist()
        y = [round(func(x).item(), 5) for x in pts]
        data = [[pts, y, 'bo']]
        pts = [pts[x] for x in range(0, nPts, ptsGap)]
        y = [y[k] for k in range(0, nPts, ptsGap)]
        data.append([pts, y, 'go'])
        # add xRange func() values
        fRange = [func(x).item() for x in xRange]
        data.append([xRange, fRange, 'ro'])

        # plot
        fig, ax = plt.subplots(nrows=1, ncols=1)
        for x, y, style in data:
            ax.plot(x, y, style)

        ax.set_xticks(pts)
        ax.set_yticks(y)
        ax.set_xlabel('flops/baselineFlops')
        ax.set_ylabel('Loss')
        ax.set_title('Flops ratio loss function')
        fig.set_size_inches(25, 10)

        pdf = PdfPages(self.flopsLossImgPath)
        pdf.savefig(fig)
        pdf.close()
