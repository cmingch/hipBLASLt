################################################################################
#
# Copyright (C) 2022-2024 Advanced Micro Devices, Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
################################################################################

from . import ClientExecutable
from . import LibraryIO
from .TensileInstructions import getGfxName, DataType, getCOVFromParam
from .Common import globalParameters, pushWorkingPath, popWorkingPath, print1, printExit, CHeader, printWarning, listToInitializer, ClientExecutionLock
from .SolutionStructs import Problem, ProblemType, ProblemSizesMock, ProblemSizesMockDummy, ActivationArgs, BiasTypeArgs, FactorDimArgs
from .TensileCreateLibrary import copyStaticFiles

import os
import subprocess
import shlex
import shutil
from enum import Enum
from glob import glob

from .Contractions import FreeIndex, BatchIndex
from .Contractions import ProblemType as ContractionsProblemType

class DataInitName(Enum):
  Zero = 0
  One = 1
  Two = 2
  Random = 3
  NaN = 4
  Inf = 5
  BadInput = 6
  BadOutput = 7
  SerialIdx = 8
  SerialDim0 = 9
  SerialDim1 = 10
  Identity = 11
  TrigSin = 12
  TrigCos = 13
  TrigAbsSin = 14
  TrigAbsCos = 15
  RandomNarrow = 16
  NegOne = 17
  Max = 18
  DenormMin = 19
  DenormMax = 20
  RandomNegPosLimited = 21
  TrigIndSin = 23
  TrigIndCos = 24
  TrigIndAbsSin = 25
  TrigIndAbsCos = 26

class ClientLogLevel(Enum):
  Error = 0
  Terse = 1
  Verbose = 2
  Debug = 3


################################################################################
# Main
################################################################################
def main(config, cxxCompiler: str, cCompiler: str):
  libraryLogicPath = os.path.join(globalParameters["WorkingPath"], \
      globalParameters["LibraryLogicPath"])
  stepBaseDir = pushWorkingPath(globalParameters["LibraryClientPath"])

  pushWorkingPath("source")
  copyStaticFiles()

  ##############################################################################
  # Read Logic Files
  ##############################################################################
  logicFiles = [os.path.join(libraryLogicPath, f) for f \
      in os.listdir(libraryLogicPath) \
      if (os.path.isfile(os.path.join(libraryLogicPath, f)) \
      and os.path.splitext(f)[1]==".yaml")]
  print1("LogicFiles: %s" % logicFiles)
  functions = []
  functionNames = []
  enableHalf = False

  createLibraryScript = getBuildClientLibraryScript(stepBaseDir, libraryLogicPath, cxxCompiler)
  subprocess.run(shlex.split(createLibraryScript), cwd=stepBaseDir)
  coList = glob(os.path.join(stepBaseDir,"library/*.co"))
  yamlList = glob(os.path.join(stepBaseDir,"library/*.yaml"))
    
  clientParametersPaths = []
  for logicFileName in logicFiles:
    (scheduleName, _, problemType, _, exactLogic, newLibrary, _) \
        = LibraryIO.parseLibraryLogicFile(logicFileName, cxxCompiler)
    if problemType["DataType"].isHalf():
        enableHalf = True
    functions.append((scheduleName, problemType))
    functionNames.append("tensile_%s" % (problemType))
    problemSizes = ProblemSizesMock(exactLogic) if exactLogic else ProblemSizesMockDummy()
    if len(problemType["BiasDataTypeList"]) > 0:
      biasTypeArgs = BiasTypeArgs(problemType, [problemType["BiasDataTypeList"][0]])
    else:
      biasTypeArgs = ""

    activationEnums = [[{'Enum': 'relu'}]]
    factorDimEnums = [0]
    # Reading the activation args from the LibraryClient section in the config YAML.
    # Example: enable relu and gelu activation and using none to run without activation
    #    LibraryClient:
    #      - ActivationArgs:
    #        - [Enum: none]
    #        - [Enum: gelu]
    #        - [Enum: relu]
    icacheFlushArgs = [False,]
    if len(config) > 0:
      for lc in config[0:]:
        if "ActivationArgs" in lc:
          activationEnums = lc["ActivationArgs"]
          break
        if "FactorDimArgs" in lc:
          factorDimEnums = lc["FactorDimArgs"]
        if "ICacheFlush" in lc:
          icacheFlushArgs = lc["ICacheFlush"]
    isForAll = True if problemType["ActivationType"] in ['all', 'hipblaslt_all'] else False
    activationArgs = ActivationArgs(problemType, activationEnums) if isForAll else ""
    factorDimArgs = FactorDimArgs(problemType, factorDimEnums)
    clientParametersPaths.append(writeClientConfig(
                                  forBenchmark=False,
                                  solutions=None,
                                  problemSizes=problemSizes,
                                  biasTypeArgs=biasTypeArgs,
                                  factorDimArgs=factorDimArgs,
                                  activationArgs=activationArgs,
                                  icacheFlushArgs=icacheFlushArgs,
                                  stepName=str(ProblemType(problemType)),
                                  stepBaseDir=globalParameters["WorkingPath"],
                                  newLibrary=newLibrary,
                                  configBase="ClientParameters_%s"%str(ProblemType(problemType)),
                                  codeObjectFiles=coList,
                                  tileAwareSelection=False,
                                  libraryFile=yamlList[0]))
  globalParameters["EnableHalf"] = enableHalf

  ##############################################################################
  # Write Generated Header
  ##############################################################################
  forBenchmark = False
  problemSizes = None
  popWorkingPath() # source

  ##############################################################################
  # Run Build Script
  ##############################################################################
  # if redo=true, clobber the build directory
  if globalParameters["ForceRedoLibraryClient"]:
    shutil.rmtree(os.path.join(globalParameters["WorkingPath"], "build"), \
        ignore_errors=True)

  forBenchmark = False
  enableTileSelection = False
  returncode = runClient(libraryLogicPath, forBenchmark, enableTileSelection, cxxCompiler, cCompiler, clientParametersPaths)

  popWorkingPath() # LibraryClient

  return returncode

################################################################################
# Write Run Script
################################################################################
def runNewClient(scriptPath, clientParametersPath, cxxCompiler: str, cCompiler: str, clientBuildDir=None):

  clientExe = ClientExecutable.getClientExecutable(cxxCompiler, cCompiler, clientBuildDir)
  iniFile = "--config-file={}".format(clientParametersPath)
  args = [clientExe, iniFile]

  try:
    subprocess.run(args, check=True)
  except (subprocess.CalledProcessError, OSError) as e:
    printWarning("ClientWriter Benchmark Process exited with error: {}".format(e))


def runClient(libraryLogicPath, forBenchmark, enableTileSelection, cxxCompiler: str, cCompiler: str, configPaths=None):
  # write runScript
  pushWorkingPath("build")
  path = globalParameters["WorkingPath"]

  runScriptName = writeRunScript(path, forBenchmark, enableTileSelection, cxxCompiler, cCompiler, configPaths)
  with ClientExecutionLock():
    process = subprocess.Popen(runScriptName, cwd=path)
    process.communicate()

  if process.returncode:
    printWarning("ClientWriter Benchmark Process exited with code %u" % process.returncode)
  popWorkingPath() # build

  return process.returncode

def getBuildClientLibraryScript(buildPath, libraryLogicPath, cxxCompiler):
  import io
  runScriptFile = io.StringIO()

  callCreateLibraryCmd = globalParameters["ScriptPath"] + "/bin/TensileCreateLibrary"


  if globalParameters["MergeFiles"]:
    callCreateLibraryCmd += " --merge-files"
  else:
    callCreateLibraryCmd += " --no-merge-files"

  if globalParameters["ShortNames"]:
    callCreateLibraryCmd += " --short-file-names"
  else:
    callCreateLibraryCmd += " --no-short-file-names"

  if globalParameters["LibraryPrintDebug"]:
    callCreateLibraryCmd += " --library-print-debug"
  else:
    callCreateLibraryCmd += " --no-library-print-debug"

  if globalParameters.get("AsmDebug", False):
    callCreateLibraryCmd += " --asm-debug"

  if globalParameters["KeepBuildTmp"]:
    callCreateLibraryCmd += " --keep-build-tmp"

  callCreateLibraryCmd += " --architecture=" + globalParameters["Architecture"]
  callCreateLibraryCmd += " --code-object-version=" + globalParameters["CodeObjectVersion"]
  callCreateLibraryCmd += " --cxx-compiler=" + cxxCompiler
  callCreateLibraryCmd += " --library-format=" + globalParameters["LibraryFormat"]

  callCreateLibraryCmd += " %s" % libraryLogicPath
  callCreateLibraryCmd += " %s" % buildPath #" ../source"
  callCreateLibraryCmd += " %s\n" % globalParameters["RuntimeLanguage"]

  runScriptFile.write(callCreateLibraryCmd)

  return runScriptFile.getvalue()

def writeBuildClientLibraryScript(path, libraryLogicPath, cxxCompiler):
  filename = os.path.join(path, \
    "build.%s" % ("bat" if os.name == "nt" else "sh") )
  with open(filename, "w") as file:
    file.write("#!/bin/bash\n\n")
    file.write("set -ex\n")
    file.write(getBuildClientLibraryScript(path, libraryLogicPath, cxxCompiler))

  if os.name != "nt":
    os.chmod(filename, 0o777)
  return filename

def writeRunScript(path, forBenchmark, enableTileSelection, cxxCompiler: str, cCompiler: str, configPaths=None):
  if configPaths is None:
    configPaths = []
    configPaths.append(os.path.join(globalParameters["WorkingPath"], "../source/ClientParameters.ini"))
    if enableTileSelection is True:
      configPaths.append(os.path.join(globalParameters["WorkingPath"], "../source/ClientParameters_Granularity.ini"))

  # create run.bat or run.sh which builds and runs
  runScriptName = os.path.join(path, \
    "run.%s" % ("bat" if os.name == "nt" else "sh") )
  runScriptFile = open(runScriptName, "w")
  if os.name != "nt":
    runScriptFile.write("#!/bin/bash\n\n")

  runScriptFile.write("set -ex\n")


  if forBenchmark:
    if os.name == "nt":
      runScriptFile.write(os.path.join(globalParameters["CMakeBuildType"], \
          "client.exe") )
    else:
      if globalParameters["PinClocks"] and globalParameters["ROCmSMIPath"]:
        runScriptFile.write("%s -d 0 --setfan 255 --setsclk 7\n" % globalParameters["ROCmSMIPath"])
        runScriptFile.write("sleep 1\n")
        runScriptFile.write("%s -d 0 -a\n" % globalParameters["ROCmSMIPath"])

      runScriptFile.write("set +e\n")


    if globalParameters["DataInitTypeA"] == -1 :
        globalParameters["DataInitTypeA"] = globalParameters["DataInitTypeAB"]
    if globalParameters["DataInitTypeB"] == -1 :
        globalParameters["DataInitTypeB"] = globalParameters["DataInitTypeAB"]

    runScriptFile.write("ERR1=0\n")

    clientExe = ClientExecutable.getClientExecutable(cxxCompiler, cCompiler)
    for configFile in configPaths:
      runScriptFile.write("{} --config-file {} {}\n".format(clientExe, configFile, globalParameters["ClientArgs"]))
    runScriptFile.write("ERR2=$?\n\n")

    runScriptFile.write("""
ERR=0
if [[ $ERR1 -ne 0 ]]
then
    echo one
    ERR=$ERR1
fi
if [[ $ERR2 -ne 0 ]]
then
    echo two
    ERR=$ERR2
fi
""")

    if os.name != "nt":
      if globalParameters["PinClocks"] and globalParameters["ROCmSMIPath"]:
        runScriptFile.write("%s -d 0 --resetclocks\n" % globalParameters["ROCmSMIPath"])
        runScriptFile.write("%s -d 0 --setfan 50\n" % globalParameters["ROCmSMIPath"])
  else:
    for configFile in configPaths:
      runScriptFile.write("{} --config-file {} {} --best-solution 1\n".format(ClientExecutable.getClientExecutable(cxxCompiler, cCompiler), configFile, globalParameters["ClientArgs"]))
  if os.name != "nt":
    runScriptFile.write("exit $ERR\n")
  runScriptFile.close()
  if os.name != "nt":
    os.chmod(runScriptName, 0o777)
  return runScriptName


def toCppBool(yamlBool):
  return "true" if yamlBool else "false"

def getMaxSolutionSizes(solutions, solutionSummationSizes):

  maxK = max(solutionSummationSizes)
  maxMT0 = 0
  maxMT1 = 0
  for solution in solutions:

    wg = solution["WorkGroup"]
    tt = solution["ThreadTile"]
    mt0 = wg[0] * tt[0]
    mt1 = wg[1] * tt[1]

    if (mt0 > maxMT0):
      maxMT0 = mt0

    if (mt1 > maxMT1):
      maxMT1 = mt1

  return [maxMT0, maxMT1, maxK]

def checkConstStride(constStrideMap, keyIdx):
  finalVal = None
  for (mapIdx, val) in constStrideMap:
    if keyIdx == mapIdx:
      finalVal = val
  #print ("idx=", keyIdx, "=", finalVal)
  return finalVal


def problemSizeParams(problemType, problem, factorDim):

    numIndices = len(problemType.indices)
    rv = []

    if problem.stridesA:
        astrides = list(problem.stridesA)
    else:
        astrides = [-1] * problemType.aDims
    for sc in problemType.setConstStrideA:
        index = problemType.indices[sc[0]]
        if type(index) == FreeIndex:
            assert(index.isA)
            astrides[index.i] = sc[1]
        else:
            astrides[index.a] = sc[1]

    if problem.stridesB:
      bstrides = list(problem.stridesB)
    else:
      bstrides = [-1] * problemType.bDims
    for sc in problemType.setConstStrideB:
        index = problemType.indices[sc[0]]
        if type(index) == FreeIndex:
            assert(not index.isA)
            bstrides[index.i] = sc[1]
        else:
            bstrides[index.b] = sc[1]

    if problem.stridesC:
      cstrides = list(problem.stridesC)
    else:
      cstrides = [-1] * problemType.cDims

    if problem.stridesD:
      dstrides = list(problem.stridesD)
    else:
      dstrides = [-1] * problemType.dDims

    if len(problem.sizes) == numIndices:
        None
    elif len(problem.sizes) == numIndices + 4:
        # FIXME-problem, this is Exact format with strides tacked onto sizes as 4 extra pams
        # should just set problem.stride* appropriately when reading the Yaml and not deal with extra fields here
        if astrides[1] == -1:
          astrides[1] = problem.sizes[numIndices+2]
        elif astrides[1] != problem.sizes[numIndices+2]:
          raise RuntimeError("problem-specified lda(%u) conflicts with setConstStrideA(%u)" % \
              (astrides[1], problem.sizes[numIndices+2]))

        if bstrides[1] == -1:
          bstrides[1] = problem.sizes[numIndices+3]
        elif bstrides[1] != problem.sizes[numIndices+3]:
          raise RuntimeError("problem-specified ldb(%u) conflicts with setConstStrideB(%u)" % \
              (bstrides[1], problem.sizes[numIndices+3]))

        if cstrides[1] == -1:
          cstrides[1] = problem.sizes[numIndices+1]

        if dstrides[1] == -1:
          dstrides[1] = problem.sizes[numIndices+0]

    else:
        raise RuntimeError(
            "Invalid number of problem type indices: {0} - Indices: {1}, problemSize: {2}".format(len(problem.sizes), numIndices,
            ', '.join(map(str, problem.sizes))))

    problemSizeArg = ('problem-size', ','.join(map(str, problem.sizes[:numIndices])))
    rv.insert(0, problemSizeArg)

    rv.append(('a-strides', ",".join(map(str, astrides))))
    rv.append(('b-strides', ",".join(map(str, bstrides))))
    if cstrides:
      rv.append(('c-strides', ",".join(map(str, cstrides))))
    if dstrides:
      rv.append(('d-strides', ",".join(map(str, dstrides))))
      if problemType.useE:
          rv.append(('e-strides', ",".join(map(str, dstrides))))
    if problemType.useBias:
      length = problem.sizes[0]
      err_str = "M"
      if problemType.sparse:
        if len(factorDim) > 1:
          length = max(problem.sizes[0], problem.sizes[1])
          err_str = "max(M,N)"
        elif 1 in factorDim:
          length = problem.sizes[1]
          err_str = "N"
      biasstrides = [1, length, 0]
      for sc in problemType.setConstStrideBias:
        index = problemType.indices[sc[0]]
        if type(index) == BatchIndex:
            biasstrides[2] = sc[1]
      if biasstrides[2] == -1:
        biasstrides[2] = length
      elif biasstrides[2] != 0 and biasstrides[2] < length:
        raise RuntimeError("problem-specified bias stride(%u) must >= %s (%u)" % \
              (biasstrides[2], err_str, length))
      rv.append(('bias-strides', ",".join(map(str, biasstrides))))

    return rv

def dataInitParams(problemType):
    initA = globalParameters['DataInitTypeA']
    initB = globalParameters['DataInitTypeB']
    initC = globalParameters['DataInitTypeC']
    initD = globalParameters['DataInitTypeD']
    initE = globalParameters['DataInitTypeE']
    initAlpha = globalParameters['DataInitTypeAlpha']
    initBeta  = globalParameters['DataInitTypeBeta']
    initBias  = globalParameters['DataInitTypeBias']
    initScaleA  = globalParameters['DataInitTypeScaleA']
    initScaleB  = globalParameters['DataInitTypeScaleB']
    initScaleC  = globalParameters['DataInitTypeScaleC']
    initScaleD  = globalParameters['DataInitTypeScaleD']
    initScaleAlphaVec  = globalParameters['DataInitTypeScaleAlphaVec']

    if not problemType.useBeta:
        initBeta = 0

    if initA == -1: initA = globalParameters['DataInitTypeAB']
    if initB == -1: initB = globalParameters['DataInitTypeAB']

    return [('init-a',             DataInitName(initA).name),
            ('init-b',             DataInitName(initB).name),
            ('init-c',             DataInitName(initC).name),
            ('init-d',             DataInitName(initD).name),
            ('init-e',             DataInitName(initE).name),
            ('init-alpha',         DataInitName(initAlpha).name),
            ('init-beta',          DataInitName(initBeta).name),
            ('init-bias',          DataInitName(initBias).name),
            ('init-scaleA',        DataInitName(initScaleA).name),
            ('init-scaleB',        DataInitName(initScaleB).name),
            ('init-scaleC',        DataInitName(initScaleC).name),
            ('init-scaleD',        DataInitName(initScaleD).name),
            ('init-scaleAlphaVec', DataInitName(initScaleAlphaVec).name)]

def boundsCheckName(mode):
    if mode == 0: return 'Disable'
    if mode == 1: return 'NaN'
    if mode == 2: return 'GuardPageFront'
    if mode == 3: return 'GuardPageBack'
    if mode == 4: return 'GuardPageAll'

def pruneModeName(mode):
    if mode == 0: return 'PruneRandom'
    if mode == 1: return 'PruneXX00'
    if mode == 2: return 'PruneX0X0'
    if mode == 3: return 'Prune0XX0'
    if mode == 4: return 'PruneX00X'
    if mode == 5: return 'Prune0X0X'
    if mode == 6: return 'Prune00XX'

def writeClientConfigIni(forBenchmark, problemSizes, biasTypeArgs, factorDimArgs, activationArgs, icacheFlushArgs, problemType, sourceDir, codeObjectFiles, resultsFileName, parametersFilePath, libraryFile=None):

    with open(parametersFilePath, "w") as f:
        def param(key, value):
            f.write("{}={}\n".format(key, value))

        if libraryFile is None:
          libraryFilename = "TensileLibrary.yaml" if globalParameters["LibraryFormat"] == "yaml" else "TensileLibrary.dat"
          libraryFile = os.path.join(sourceDir, "library", libraryFilename)
        param("library-file", libraryFile)

        currentGFXName = getGfxName(globalParameters["CurrentISA"])
        for coFile in codeObjectFiles:
            if 'gfx' not in coFile or currentGFXName in coFile:
                param("code-object", os.path.join(sourceDir,coFile))

        param('results-file', resultsFileName)
        param('performance-metric', globalParameters["PerformanceMetric"])
        param('problem-identifier', problemType.operationIdentifier)
        param('compute-input-type', problemType.computeInputType.toEnum())
        param('a-type',     problemType.aType.toEnum())
        param('b-type',     problemType.bType.toEnum())
        param('c-type',     problemType.cType.toEnum())
        param('d-type',     problemType.dType.toEnum())
        if problemType.useE:
            param('e-type',     problemType.eType.toEnum())
        if problemType.outputAmaxD:
            param('amaxD-type',     problemType.amaxDType.toEnum())
        param('alpha-type', problemType.alphaType.toEnum())
        param('beta-type',  problemType.betaType.toEnum())
        param('f32-xdl-math-op', problemType.f32XdlMathOp.toEnum())
        param('activation-compute-type', problemType.activationComputeDataType.toEnum())
        param('use-gradient', problemType.useGradient)
        param('use-bias',   problemType.useBias)
        param('bias-source',   problemType.biasSrcWhiteList[0])
        param('use-e', problemType.useE)
        param('output-amaxD', problemType.outputAmaxD)
        param('use-scaleAB',   problemType.useScaleAB)
        param('use-scaleCD',   problemType.useScaleCD)
        param('use-scaleAlphaVec',   problemType.useScaleAlphaVec)
        param('swizzle-tensor-a', problemType.swizzleTensorA)
        param('swizzle-tensor-b', problemType.swizzleTensorB)
        if biasTypeArgs:
          for btype in biasTypeArgs.biasTypes:
            param('bias-type-args',  btype.toEnum())
        if factorDimArgs:
          for fdim in factorDimArgs.factorDims:
            param('factor-dim-args', fdim)


        if icacheFlushArgs:
          for opt in icacheFlushArgs:
            param('icache-flush-args', opt)

        param('sparse',   problemType.sparse)
        param('high-precision-accumulate', problemType.highPrecisionAccumulate)
        param('strided-batched', problemType.stridedBatched)
        param('grouped-gemm', problemType.groupedGemm)

        for problem in problemSizes.problems:
            for key,value in problemSizeParams(problemType, problem, factorDimArgs.factorDims):
                param(key,value)

        if activationArgs:
          for setting in activationArgs.settingList:
            param('activation-enum-args', setting.activationEnum.toEnum())
        param('activation-type', problemType.activationType.toEnum())
        param('activation-no-guard', problemType.activationNoGuard)
        if globalParameters["DataInitValueActivationArgs"]:
          param('activation-additional-args', ','.join(map(str, globalParameters["DataInitValueActivationArgs"])))

        param("device-idx",               globalParameters["Device"])

        param("init-seed",                globalParameters["DataInitSeed"])

        for key,value in dataInitParams(problemType):
            param(key, value)

        param("c-equal-d",                globalParameters["CEqualD"])

        if globalParameters["PrintTensorA"]:
          param("print-tensor-a",         1)
        if globalParameters["PrintTensorB"]:
          param("print-tensor-b",         1)
        if globalParameters["PrintTensorC"]:
          param("print-tensor-c",         1)
        if globalParameters["PrintTensorD"]:
          param("print-tensor-d",         1)
        if globalParameters["PrintTensorRef"]:
          param("print-tensor-ref",       1)
        if globalParameters["PrintTensorBias"]:
          param("print-tensor-bias",      1)
        if globalParameters["PrintTensorAmaxD"]:
          param("print-tensor-amaxd",      1)
        if globalParameters["DumpTensors"]:
          param("dump-tensors",           1)
        if globalParameters["ExitOnFails"] > 1:
          param("exit-on-error", 1)

        param('prune-mode',               pruneModeName(int(globalParameters["PruneSparseMode"])))
        param("bounds-check",             boundsCheckName(int(globalParameters["BoundsCheck"])))
        param("print-valids",             globalParameters["ValidationPrintValids"])
        param("print-max",                globalParameters["ValidationMaxToPrint"])
        param("num-benchmarks",           globalParameters["NumBenchmarks"])

        numElementsToValidate = globalParameters["NumElementsToValidate"]
        if not forBenchmark:
         if globalParameters["NumElementsToValidateWinner"] == -1 or numElementsToValidate == -1:
           numElementsToValidate = -1
         else:
           numElementsToValidate = max(globalParameters["NumElementsToValidateWinner"], globalParameters["NumElementsToValidate"])
        param("num-elements-to-validate", numElementsToValidate)
        param("num-enqueues-per-sync",    globalParameters["EnqueuesPerSync"])
        param("max-enqueues-per-sync",    globalParameters["MaxEnqueuesPerSync"])
        param("num-syncs-per-benchmark",  globalParameters["SyncsPerBenchmark"])
        param("skip-slow-solution-ratio", globalParameters["SkipSlowSolutionRatio"])
        param("use-gpu-timer",            globalParameters["KernelTime"])
        param("hardware-monitor",         globalParameters["HardwareMonitor"])
        param("num-warmups",              globalParameters["NumWarmups"])
        param("min-flops-per-sync",       globalParameters["MinFlopsPerSync"])
        param("sleep-percent",            globalParameters["SleepPercent"])
        param("perf-l2-read-hits",        globalParameters["PerfModelL2ReadHits"])
        param("perf-l2-write-hits",       globalParameters["PerfModelL2WriteHits"])
        param("perf-l2-read-bw-mul",      globalParameters["PerfModelL2ReadBwMul"])
        param("perf-read-efficiency",     globalParameters["PerfModelReadEfficiency"])
        param("csv-export-extra-cols",    globalParameters["CSVExportWinner"])
        param("csv-merge-same-problems",  globalParameters["CSVMergeSameProblemID"])
        param("log-level",                ClientLogLevel(globalParameters["ClientLogLevel"]).name)
        param("max-workspace-size",       globalParameters["MaxWorkspaceSize"])
        param("PrintWinnersOnly",         globalParameters["PrintWinnersOnly"])
        param("granularity-threshold",    globalParameters["GranularityThreshold"])
        param("pristine-on-gpu",          globalParameters["PristineOnGPU"])

        param("library-update-file",      globalParameters["LibraryUpdateFile"])
        param("library-update-comment",   globalParameters["LibraryUpdateComment"])

        param("use-user-args",            globalParameters["UseUserArgs"])
        param("rotating-buffer-size",     globalParameters["RotatingBufferSize"])
        param("rotating-buffer-mode",     globalParameters["RotatingMode"])


def writeClientConfig(forBenchmark, solutions, problemSizes, biasTypeArgs, factorDimArgs, activationArgs, icacheFlushArgs, stepName, stepBaseDir, newLibrary, codeObjectFiles, tileAwareSelection, configBase = "ClientParameters", libraryFile = None):

    if tileAwareSelection:
      filename = os.path.join(globalParameters["WorkingPath"], "%s_Granularity.ini"%configBase)
    else:
      filename = os.path.join(globalParameters["WorkingPath"], "%s.ini"%configBase)

    if len(newLibrary.solutions)==0:
      raise RuntimeError ("No valid solutions found")

    resultsFileName = None
    if tileAwareSelection:
      resultsFileName = os.path.join(stepBaseDir, "../Data", stepName+"_Granularity.csv")
    else:
      resultsFileName = os.path.join(stepBaseDir, "../Data", stepName+".csv")

    newSolution = next(iter(newLibrary.solutions.values()))
    sourceDir = os.path.join(stepBaseDir, "source")
    writeClientConfigIni(forBenchmark, problemSizes, biasTypeArgs, factorDimArgs, activationArgs, icacheFlushArgs, newSolution.problemType, sourceDir, codeObjectFiles, resultsFileName, filename, libraryFile)

    return filename

def CreateBenchmarkClientParametersForSizes(libraryRootPath, problemSizes, dataFilePath, configFile, problemTypeDict=None):

    libraryPath = os.path.join(libraryRootPath, "library")
    libraryFiles = [os.path.join(libraryPath, f) for f in os.listdir(libraryPath)]
    codeObjectFiles = [f for f in libraryFiles if f.endswith("co")]

    if problemTypeDict:
      problemType = ContractionsProblemType.FromOriginalState(problemTypeDict)
    else:
      # if the we can library contains meta data then we can get the problem type this data
      metaDataFilePath = os.path.join(libraryPath, "metadata.yaml")
      if not os.path.exists(metaDataFilePath):
        printExit ("meta data file %s does not exist" % metaDataFilePath)
      metaData = LibraryIO.read(metaDataFilePath)
      problemTypeDict = metaData["ProblemType"]
      problemType = ContractionsProblemType.FromOriginalState(problemTypeDict)

    writeClientConfigIni(True, problemSizes, "", "", "", "", problemType, libraryRootPath, codeObjectFiles, dataFilePath, configFile)


################################################################################
# Write Generated Benchmark Parameters
################################################################################
def writeClientParameters(forBenchmark, solutions, problemSizes, stepName, \
    functionList, stepBaseDir, solutionSummationSizes, solutionWriter = None):
  h = ""

  ##############################################################################
  # Min Naming
  ##############################################################################
  """
  if forBenchmark:
    kernels = []
    for solution in solutions:
      solutionKernels = solution.getKernels()
      for kernel in solutionKernels:
        if kernel not in kernels:
          kernels.append(kernel)

    solutionSerialNaming = Solution.getSerialNaming(solutions)
    kernelSerialNaming = Solution.getSerialNaming(kernels)
    solutionMinNaming = Solution.getMinNaming(solutions)
    kernelMinNaming = Solution.getMinNaming(kernels)
  """

  if forBenchmark:
    if globalParameters["MergeFiles"]:
      h += "#include \"Solutions.h\"\n"
    else:
      for solution in solutions:
        solutionName = solutionWriter.getSolutionName(solution)
        h += "#include \"" + solutionName + ".h\"\n"
        h += "#include \"Solutions.h\"\n"
    h += "#include \"ReferenceCPU.h\"\n"
    h += "\n"
  else:
    h += "#include \"Solutions.h\"\n"
    h += "#include \"Tensile.h\"\n"


  h += "typedef enum {\n"
  h += "    enum_float,\n"
  h += "    enum_double,\n"
  h += "    enum_TensileComplexFloat,\n"
  h += "    enum_TensileComplexDouble\n"
  h += "#ifdef Tensile_ENABLE_HALF\n"
  h += "    ,enum_TensileHalf\n"
  h += "#endif\n"
  h += "    ,enum_TensileInt8x4\n"
  h += "    ,enum_TensileInt32\n"
  h += "    ,enum_tensile_bfloat16\n"
  h += "} DataTypeEnum;\n"
  h += "\n"

  h += "// Debug Params\n"
  h += "const unsigned printTensorA=%x;\n" % int(globalParameters["PrintTensorA"])
  h += "const unsigned printTensorB=%x;\n" % int(globalParameters["PrintTensorB"])
  h += "const unsigned printTensorC=%x;\n" % int(globalParameters["PrintTensorC"])
  h += "const unsigned printTensorD=%x;\n" % int(globalParameters["PrintTensorD"])

  h += "const bool printWinnersOnly=%s;\n" % toCppBool(globalParameters["PrintWinnersOnly"])
  h += "\n"

  h += "const char indexChars[%u] = \"%s" \
      % (len(globalParameters["IndexChars"])+1, \
      globalParameters["IndexChars"][0])
  for i in range(1, len(globalParameters["IndexChars"])):
    h += globalParameters["IndexChars"][i]
  h += "\";\n"

  h += "unsigned int functionIdx;\n"
  h += "unsigned int dataTypeIdx;\n"
  h += "unsigned int problemTypeIdx;\n"
  h += "\n"

  ##############################################################################
  # Problem Types
  ##############################################################################
  #dataTypes = []
  #problemTypes = []
  #functionSerialToDataTypeAndIdx = []
  dataTypes = []
  problemTypes = []
  destDataTypes = {}
  computeDataTypes = {}
  problemTypesForDataType = {} # for data type
  schedulesForProblemType = {} # for problem type
  functionInfo = [] # dataTypeIdx, problemTypeIdx, idxWithinDataType, idxWithinProblemType
  #tileSelection = False

  if forBenchmark:
    problemType = solutions[0]["ProblemType"]
    dataType = problemType["DataType"]
    #tileSelection = problemType["TileAwareSelection"]

    destDataType = problemType["DestDataType"]
    destDataTypes[dataType] = destDataType

    computeDataType = problemType["ComputeDataType"]
    computeDataTypes[dataType] = computeDataType

    dataTypes.append(dataType)

    problemTypes.append(problemType)
    problemTypesForDataType[dataType] = [problemType]
    schedulesForProblemType[problemType] = solutions
    numProblemTypes = 1
    for solution in solutions:
      functionInfo.append([ 0, 0, 0, 0, 0, 0 ])
  else:
    for functionIdx in range(0, len(functionList)):
      function = functionList[functionIdx]
      scheduleName = function[0]
      problemType = function[1]
      dataType = problemType["DataType"]
      destDataType = problemType["DestDataType"]
      computeDataType = problemType["ComputeDataType"]
      if dataType not in dataTypes:
        dataTypes.append(dataType)
        destDataTypes[dataType] = destDataType
        computeDataTypes[dataType] = computeDataType
        problemTypesForDataType[dataType] = []
      if problemType not in problemTypesForDataType[dataType]:
        problemTypesForDataType[dataType].append(problemType)
        schedulesForProblemType[problemType] = []
      schedulesForProblemType[problemType].append(scheduleName)

    # sort
    dataTypes = sorted(dataTypes)
    for dataType in dataTypes:
      problemTypesForDataType[dataType] = \
          sorted(problemTypesForDataType[dataType],key=str)
      for problemType in problemTypesForDataType[dataType]:
        schedulesForProblemType[problemType] = \
            sorted(schedulesForProblemType[problemType],key=str)

    # assign info
    functionIdxSerial = 0
    problemTypeIdxSerial = 0
    for dataTypeIdxSerial in range(0, len(dataTypes)):
      dataType = dataTypes[dataTypeIdxSerial]
      functionIdxForDataType = 0
      for problemTypeIdxForDataType in range(0, \
          len(problemTypesForDataType[dataType])):
        problemType = \
            problemTypesForDataType[dataType][problemTypeIdxForDataType]
        problemTypes.append(problemType)
        functionIdxForProblemType = 0
        for functionIdxForProblemType in range(0, \
            len(schedulesForProblemType[problemType])):
          functionInfo.append([ \
              dataTypeIdxSerial, \
              problemTypeIdxForDataType, \
              problemTypeIdxSerial, \
              functionIdxSerial,\
              functionIdxForDataType,\
              functionIdxForProblemType, \
              ])
          functionIdxForProblemType += 1
          functionIdxForDataType += 1
          functionIdxSerial += 1
        problemTypeIdxSerial += 1
    numProblemTypes = problemTypeIdxSerial
    numFunctions = functionIdxSerial
    h += "const unsigned int numFunctions = %u;\n" % numFunctions

  ##############################################################################
  # Data Types
  ##############################################################################
  h += "/* data types */\n"
  numDataTypes = len(dataTypes)
  h += "const unsigned int numDataTypes = %u;\n" % numDataTypes
  h += "const DataTypeEnum dataTypeEnums[numDataTypes] = { enum_%s" \
      % dataTypes[0].toCpp()
  for dataTypeIdx in range(1, numDataTypes):
    h += ", enum_%s" % dataTypes[dataTypeIdx].toCpp()
  h += " };\n"
  # bytes per elements
  h += "const unsigned int bytesPerElement[numDataTypes] = { %u" \
      % (dataTypes[0].numBytes())
  for dataTypeIdx in range(1, numDataTypes):
    dataType = dataTypes[dataTypeIdx]
    h += ", %u" % dataType.numBytes()
  h += " };\n"
  # flops per mac
  if dataTypes[0].isInt8x4():
    h += "const unsigned int numFlopsPerMac[numDataTypes] = { %u" % (8 if dataTypes[0].isReal() else 32)
  else:
    h += "const unsigned int numFlopsPerMac[numDataTypes] = { %u" % (2 if dataTypes[0].isReal() else 8)
  for dataTypeIdx in range(1, numDataTypes):
    dataType = dataTypes[dataTypeIdx]
    h += ", %u" % (2 if dataType.isReal() else 8)
  h += " };\n"
  for dataTypeIdx in range(0, numDataTypes):
    h += "#define Tensile_DATA_TYPE_%s\n" \
        % dataTypes[dataTypeIdx].toCpp().upper()

  ##############################################################################
  # Problem Types
  ##############################################################################
  h += "/* problem types */\n"
  h += "const unsigned int numProblemTypes = %u;\n" % numProblemTypes
  # Num C Indices
  h += "const unsigned int numIndicesC[numProblemTypes] = { %u" \
      % problemTypes[0]["NumIndicesC"]
  for problemTypeIdx in range(1, numProblemTypes):
    problemType = problemTypes[problemTypeIdx]
    h += ", %u" % problemType["NumIndicesC"]
  h += " };\n"

  # Num AB Indices
  maxNumIndicesA = len(problemTypes[0]["IndexAssignmentsA"])
  maxNumIndicesB = len(problemTypes[0]["IndexAssignmentsB"])
  h += "const unsigned int numIndicesA[numProblemTypes] = { %u" \
      % len(problemTypes[0]["IndexAssignmentsA"])
  for problemTypeIdx in range(1, numProblemTypes):
    problemType = problemTypes[problemTypeIdx]
    numIndicesA = len(problemType["IndexAssignmentsA"])
    h += ", %u" % numIndicesA
    maxNumIndicesA = max(numIndicesA, maxNumIndicesA)
  h += " };\n"
  h += "const unsigned int maxNumIndicesA = %u;\n" % maxNumIndicesA

  h += "const unsigned int numIndicesB[numProblemTypes] = { %u" \
      % len(problemTypes[0]["IndexAssignmentsB"])
  for problemTypeIdx in range(1, numProblemTypes):
    problemType = problemTypes[problemTypeIdx]
    numIndicesB = len(problemType["IndexAssignmentsB"])
    h += ", %u" % numIndicesB
    maxNumIndicesB = max(numIndicesB, maxNumIndicesB)
  h += " };\n"
  h += "const unsigned int maxNumIndicesB = %u;\n" % maxNumIndicesB

  # Index Assignments A
  h += "const unsigned int indexAssignmentsA[numProblemTypes][maxNumIndicesA] = {\n"
  for problemTypeIdx in range(0, numProblemTypes):
    problemType = problemTypes[problemTypeIdx]
    indices = problemType["IndexAssignmentsA"]
    h += "  { %u" % indices[0]
    for i in range(1, maxNumIndicesA):
      if i < len(indices):
        h += ", %u" % indices[i]
      else:
        h += ", static_cast<unsigned int>(-1)"
    if problemTypeIdx < numProblemTypes-1:
      h += " },\n"
    else:
      h += " }\n"
  h += "};\n"
  # Index Assignments B
  h += "const unsigned int indexAssignmentsB[numProblemTypes][maxNumIndicesB] = {\n"
  for problemTypeIdx in range(0, numProblemTypes):
    problemType = problemTypes[problemTypeIdx]
    indices = problemType["IndexAssignmentsB"]
    h += "  { %u" % indices[0]
    for i in range(1, maxNumIndicesB):
      if i < len(indices):
        h += ", %u" % indices[i]
      else:
        h += ", static_cast<unsigned int>(-1)"
    if problemTypeIdx < numProblemTypes-1:
      h += " },\n"
    else:
      h += " }\n"
  h += "};\n"
  # Index Assignments LD
  h += "const unsigned int numIndicesLD = %u;\n" % problemType["NumIndicesLD"]
  h += "const unsigned int indexAssignmentsLD[numIndicesLD] = {"
  if problemType["NumIndicesLD"] > 0:
    h += " %u" % problemType["IndexAssignmentsLD"][0]
    for ldIdx in range(1, len(problemType["IndexAssignmentsLD"])):
      h += ", %u" % problemType["IndexAssignmentsLD"][ldIdx]
  h += "};\n"
  # beta
  h += "bool useBeta[numProblemTypes] = { %s" \
      % ("true" if problemTypes[0]["UseBeta"] else "false")
  for problemTypeIdx in range(1, numProblemTypes):
    problemType = problemTypes[problemTypeIdx]
    h += ", %s" % ("true" if problemType["UseBeta"] else "false")
  h += " };\n"
  # Complex Conjugates
  h += "const bool complexConjugateA[numProblemTypes] = { %s" \
      % ("true" if problemTypes[0]["ComplexConjugateA"] else "false" )
  for problemTypeIdx in range(1, numProblemTypes):
    problemType = problemTypes[problemTypeIdx]
    h += ", %s" % ("true" if problemTypes[0]["ComplexConjugateA"] else "false" )
  h += " };\n"
  h += "const bool complexConjugateB[numProblemTypes] = { %s" \
      % ("true" if problemTypes[0]["ComplexConjugateB"] else "false" )
  for problemTypeIdx in range(1, numProblemTypes):
    problemType = problemTypes[problemTypeIdx]
    h += ", %s" % ("true" if problemTypes[0]["ComplexConjugateB"] else "false" )
  h += " };\n"
  h += "\n"

  if not forBenchmark:
    h += "// dataTypeIdxSerial, problemTypeIdxForDataType, problemTypeIdxSerial, functionIdxSerial, functionIdxForDataType, functionIdxForProblemType\n"
    first = True
    h += "const unsigned int functionInfo[numFunctions][6] = {\n"
    for info in functionInfo:
      h += "%s{ %u, %u, %u, %u, %u, %u }" % ("  " if first else ",\n  ", \
          info[0], info[1], info[2], info[3], info[4], info[5] )
      first = False
    h += " };\n"


  ##############################################################################
  # Problem Sizes
  ##############################################################################
  maxNumIndices = problemTypes[0]["TotalIndices"]
  if not forBenchmark:
    for problemType in problemTypes:
      maxNumIndices = max(problemType["TotalIndices"], maxNumIndices)
  h += "const unsigned int maxNumIndices = %u;\n" % maxNumIndices
  h += "const unsigned int totalIndices[numProblemTypes] = { %u" \
      % problemTypes[0]["TotalIndices"]
  for problemTypeIdx in range(1, numProblemTypes):
      h += ", %u" % problemTypes[problemTypeIdx]["TotalIndices"]
  h += " };\n"
  if forBenchmark:
    h += "const unsigned int numProblems = %u;\n" \
        % problemSizes.totalProblemSizes
    h += "const unsigned int problemSizes[numProblems][%u] = {\n" \
        % (problemTypes[0]["TotalIndices"] + problemType["NumIndicesLD"])
    for i in range(problemSizes.totalProblemSizes):
      #assert problemSizes.problems[i].stridesA == None # new stride functionality only supported on new client, not here
      problemSize = problemSizes.problems[i].sizes
      line = "  {%5u" %problemSize[0]
      for j in range(1, problemTypes[0]["TotalIndices"] + problemType["NumIndicesLD"]):
        line += ",%5u" % problemSize[j]
      line += " }"
      h += line
      if i < problemSizes.totalProblemSizes-1:
        h += ","
      else:
        h += ""
    h += "};\n"
    h += "const unsigned int minStrides[%u] = {" \
        % problemTypes[0]["TotalIndices"]
    for i in range(0, len(problemSizes.minStrides)):
      if (i!=0):
        h += ", "
      h += str(problemSizes.minStrides[i])
    h += "};\n"
  else:
    h += "unsigned int userSizes[maxNumIndices];\n"
    h += "unsigned int minStrides[%u] = {" \
        % maxNumIndices
    for i in range(0, maxNumIndices):
      if (i!=0):
        h += ", "
      h += str(0); # always use 0 for minStrides in benchmark mode
    h += "};\n"

  if forBenchmark:
    h += "/* problem sizes */\n"
    """
    h += "const bool indexIsSized[maxNumIndices] = {"
    for i in range(0, problemSizes.totalIndices):
      h += " %s" % ("true" if problemSizes.indexIsSized[i] else "false")
      if i < problemSizes.totalIndices-1:
        h += ","
    h += " };\n"

    h += "const unsigned int numIndicesSized = %u;\n" \
        % len(problemSizes.indicesSized)
    h += "const unsigned int indicesSized[numIndicesSized][4] = {\n"
    h += "// { min, stride, stride_incr, max }\n"
    for i in range(0, len(problemSizes.indicesSized)):
      r = problemSizes.indicesSized[i]
      h += "  { %u, %u, %u, %u }" % (r[0], r[1], r[2], r[3])
      if i < len(problemSizes.indicesSized)-1:
        h += ","
      h += "\n"
    h += "  };\n"

    numIndicesMapped = len(problemSizes.indicesMapped)
    h += "const unsigned int numIndicesMapped = %u;\n" % numIndicesMapped
    if numIndicesMapped > 0:
      h += "#define Tensile_INDICES_MAPPED 1\n"
      h += "const unsigned int indicesMapped[numIndicesMapped] = {"
      for i in range(0, numIndicesMapped):
        h += " %u" % problemSizes.indicesMapped[i]
        if i < numIndicesMapped-1:
          h += ","
      h += " };\n"
    else:
      h += "#define Tensile_INDICES_MAPPED 0\n"
    """

  ##############################################################################
  # Max Problem Sizes
  ##############################################################################
  if forBenchmark:
    maximumD = problemSizes.maxD
    maximumC = problemSizes.maxC
    maximumA = problemSizes.maxA
    maximumB = problemSizes.maxB
    maximumW = problemSizes.maxD * 32;

    maxMT = getMaxSolutionSizes(solutions, solutionSummationSizes)

    maxMN = 1296 * maxMT[0] * maxMT[1]
    maxMK = 36 * maxMT[0] * maxMT[2]
    maxNK = 36 * maxMT[1] * maxMT[2]

    maximumA = max(maximumA, maxMK)
    maximumB = max(maximumB, maxNK)
    maximumC = max(maximumC, maxMN)
    maximumD = max(maximumD, maxMN)
    maximumW = max(maximumW, maxMN)

    h += "size_t maxSizeD = %u;\n" % (maximumD)
    h += "size_t maxSizeC = %u;\n" % (maximumC)
    h += "size_t maxSizeA = %u;\n" % (maximumA)
    h += "size_t maxSizeB = %u;\n" % (maximumB)
    h += "size_t maxSizeW = %u;\n" % (maximumW)
    h += "\n"
  else:
    h += "size_t maxSizeD;\n"
    h += "size_t maxSizeC;\n"
    h += "size_t maxSizeA;\n"
    h += "size_t maxSizeB;\n"
    h += "size_t maxSizeW;\n"
    h += "\n"

  ##############################################################################
  # Current Problem Size
  ##############################################################################
  h += "/* current problem size */\n"
  #h += "unsigned int fullSizes[maxNumIndices];\n"
    #h += "unsigned int currentSizedIndexSizes[numIndicesSized];\n"
    #h += "unsigned int currentSizedIndexIncrements[numIndicesSized];\n"
  h += "\n"

  ##############################################################################
  # Solutions
  ##############################################################################
  if forBenchmark:
    # Solution Ptrs
    h += "/* solutions */\n"
    # Problem Type Indices
    h += "const unsigned int maxNumSolutions = %u;\n" % len(solutions)
    h += "float solutionPerf[numProblems][maxNumSolutions]; // milliseconds\n"
    h += "\n"

    h += "static const SolutionInfo solutions[maxNumSolutions] = {\n"
    for i in range(0, len(solutions)):
      solution = solutions[i]
      solutionName = solutionWriter.getSolutionName(solution)
      h += "  {(void*)%s, \"%s\", {%d, %d, %d, %d, %s} }" % \
        (solutionName, solutionName,
          solution["AssertSummationElementMultiple"],
          solution["AssertFree0ElementMultiple"],
          solution["AssertFree1ElementMultiple"],
          "false"
          )
      if i < len(solutions)-1:
        h += ","
      h += "\n"
    h += " };\n"
    h += "\n"

    numSummations = len(solutionSummationSizes)
    h += "const unsigned int numSummations = %d;\n" % (numSummations)

    h += "const unsigned int summations[numSummations] = {%d" % (solutionSummationSizes[0])
    for i in range(1, numSummations):
      h += ", %d" % (solutionSummationSizes[i])
    h += "};\n"

  ##############################################################################
  # Solution meta data
  ##############################################################################

    transA = solutions[0]["ProblemType"]["TransposeA"]
    transB = solutions[0]["ProblemType"]["TransposeB"]
    h += "const unsigned int solutionMetaData[maxNumSolutions][10] = {\n"
    for i in range(0, len(solutions)):
      solution = solutions[i]

      wg = solution["WorkGroup"]
      tt = solution["ThreadTile"]
      mt0 = wg[0] * tt[0]
      mt1 = wg[1] * tt[1]
      gsu = solution["GlobalSplitU"]
      lsu = wg[2]

      h += "  {%d, %d, %d, %d, %d, %d, %d, %d, %d, %d}" % (mt0,mt1,tt[0],tt[1],wg[0],wg[1],transA,transB,gsu,lsu)

      if (i < len(solutions) - 1):
        h += ",\n"
      else:
        h += "\n"
    h += " };\n"
    h += "\n"



  else:
    # Function Names
    functionNames = []
    for dataType in dataTypes:
      for problemType in problemTypesForDataType[dataType]:
        # example scheduleName is fiji, vega10, etc
        for scheduleName in schedulesForProblemType[problemType]:
          functionNames.append("tensile_%s" % (problemType))
    h += "const char *functionNames[numFunctions] = {\n"
    for functionIdx in range(0, len(functionNames)):
      functionName = functionNames[functionIdx]
      h += "    \"%s\"%s\n" % (functionName, \
          "," if functionIdx < len(functionNames)-1 else "" )
    h += " };\n"

  ##############################################################################
  # Runtime Structures
  ##############################################################################
  h += "/* runtime structures */\n"
  h += "TensileStatus status;\n"
  if globalParameters["RuntimeLanguage"] == "OCL":
    h += "cl_platform_id platform;\n"
    h += "cl_device_id device;\n"
    h += "cl_context context;\n"
    h += "cl_command_queue stream;\n"
  else:
    h += "hipStream_t stream;\n"
    #h += "int deviceIdx = %u;\n" \
    #    % (globalParameters["Device"])
  h += "\n"
  h += "void *deviceWS;\n"
  h += "void *deviceD;\n"
  h += "void *deviceC;\n"
  h += "void *deviceA;\n"
  h += "void *deviceB;\n"

  ##############################################################################
  # Benchmarking and Validation Parameters
  ##############################################################################
  h += "\n/* benchmarking parameters */\n"
  #h += "const bool measureKernelTime = %s;\n" \
  #    % ("true" if globalParameters["KernelTime"] else "false")
  #h += "const unsigned int numEnqueuesPerSync = %u;\n" \
  #    % (globalParameters["EnqueuesPerSync"])
  #h += "const unsigned int numSyncsPerBenchmark = %u;\n" \
  #    % (globalParameters["SyncsPerBenchmark"])
  #h += "unsigned int numElementsToValidate = %s;\n" \
  #    % (str(globalParameters["NumElementsToValidate"]) \
  #    if globalParameters["NumElementsToValidate"] >= 0 \
  #    else "0xFFFFFFFF" )
  #h += "unsigned int validationMaxToPrint = %u;\n" \
  #    % globalParameters["ValidationMaxToPrint"]
  #h += "bool validationPrintValids = %s;\n" \
  #    % ("true" if globalParameters["ValidationPrintValids"] else "false")
  h += "size_t validationStride;\n"
  if problemType["HighPrecisionAccumulate"]:
    h += "static bool useHighPrecisionAccumulate = true;\n"
  else:
    h += "static bool useHighPrecisionAccumulate = false;\n"
  #h += "unsigned int dataInitTypeC = %s;\n" % globalParameters["DataInitTypeC"]
  #h += "unsigned int dataInitTypeAB = %s;\n" % globalParameters["DataInitTypeAB"]
  h += "\n"

  ##############################################################################
  # Generated Call to Reference
  ##############################################################################
  h += "/* generated call to reference */\n"
  h += "template<typename DataType, typename DestDataType, typename ComputeDataType>\n"
  h += "TensileStatus generatedCallToReferenceCPU(\n"
  h += "    const unsigned int *sizes,\n"
  h += "    const unsigned int *minStrides,\n"
  h += "    DestDataType *referenceD,\n"
  h += "    DestDataType *referenceC,\n"
  h += "    DataType *initialA,\n"
  h += "    DataType *initialB,\n"
  h += "    const unsigned int lda,\n"
  h += "    const unsigned int ldb,\n"
  h += "    const unsigned int ldc,\n"
  h += "    const unsigned int ldd,\n"
  h += "    const unsigned int stride_a,\n"
  h += "    const unsigned int stride_b,\n"
  h += "    const unsigned int stride_c,\n"
  h += "    const unsigned int stride_d,\n"
  h += "    ComputeDataType alpha,\n"
  h += "    ComputeDataType beta,\n"
  h += "    bool useHighPrecisionAccumulate) {\n"
  h += "  return tensileReferenceCPU(\n"
  h += "      referenceD,\n"
  h += "      referenceC,\n"
  h += "      initialA,\n"
  h += "      initialB,\n"
  h += "      lda,\n"
  h += "      ldb,\n"
  h += "      ldc,\n"
  h += "      ldd,\n"
  h += "      stride_a,\n"
  h += "      stride_b,\n"
  h += "      stride_c,\n"
  h += "      stride_d,\n"
  h += "      alpha,\n"
  h += "      beta,\n"
  h += "      totalIndices[problemTypeIdx],\n"
  h += "      sizes,\n"
  h += "      minStrides,\n"
  h += "      numIndicesC[problemTypeIdx],\n"
  h += "      numIndicesA[problemTypeIdx],\n"
  h += "      numIndicesB[problemTypeIdx],\n"
  h += "      indexAssignmentsA[problemTypeIdx],\n"
  h += "      indexAssignmentsB[problemTypeIdx],\n"
  h += "      complexConjugateA[problemTypeIdx],\n"
  h += "      complexConjugateB[problemTypeIdx],\n"
  h += "      validationStride,\n"
  h += "      useHighPrecisionAccumulate);\n"
  h += "};\n"
  h += "\n"

  ##############################################################################
  # Generated Call to Solution
  ##############################################################################
  if forBenchmark:
    problemType = solutions[0]["ProblemType"]
    h += "/* generated call to solution */\n"
    h += "template<typename ComputeDataType, class SolutionInfoType>\n"
    h += "TensileStatus generatedCallToSolution(\n"
    h += "    const SolutionInfoType &solution,\n"
    h += "    SolutionLock *solutionLock,\n"
    h += "    const unsigned int *sizes,\n"
    h += "    const unsigned int *minStrides,\n"
    h += "    const unsigned int lda,\n"
    h += "    const unsigned int ldb,\n"
    h += "    const unsigned int ldc,\n"
    h += "    const unsigned int ldd,\n"
    h += "    const unsigned int stride_a,\n"
    h += "    const unsigned int stride_b,\n"
    h += "    const unsigned int stride_c,\n"
    h += "    const unsigned int stride_d,\n"
    h += "    ComputeDataType alpha,\n"
    h += "    ComputeDataType beta,\n"
    h += "    unsigned int numEvents = 0,\n"
    if globalParameters["RuntimeLanguage"] == "OCL":
      h += "    cl_event *event_wait_list = NULL,\n"
      h += "    cl_event *outputEvent = NULL ) {\n"
    else:
      h += "    hipEvent_t *startEvent = NULL,\n"
      h += "    hipEvent_t *stopEvent = NULL ) {\n"

    h += "  // calculate parameters assuming packed data\n"
    # strides
    indexChars = globalParameters["IndexChars"]
    firstStride = 1
    #assert(not problemType["UseInitialStridesCD"]) # not supported in old client
    if problemType["UseInitialStridesAB"]:
      firstStride = 0
    lastStrideD = problemType["NumIndicesC"]
    lastStrideC = problemType["NumIndicesC"]
    lastStrideA = len(problemType["IndexAssignmentsA"])
    lastStrideB = len(problemType["IndexAssignmentsB"])

    # calculate strides
    for i in range(0,lastStrideD):
      h += "  unsigned int strideD%u%s = 1" % (i, indexChars[i])
      for j in range(0, i):
        h += " * ("
        if j == 0:
          h += "(ldd != std::numeric_limits<unsigned int>::max()) ? ldd : "
        h += "std::max(minStrides[%i], sizes[%i]))" % (j,j)
      h += ";\n"
    h += "  if (stride_d != std::numeric_limits<unsigned int>::max())  strideD%u%s = stride_d;\n" % (lastStrideD-1, indexChars[lastStrideD-1])
    for i in range(0,lastStrideC):
      h += "  unsigned int strideC%u%s = 1 " % (i, indexChars[i])
      for j in range(0, i):
        h += " * ("
        if j == 0:
          h += "(ldc != std::numeric_limits<unsigned int>::max()) ? ldc : "
        h+= "std::max(minStrides[%i], sizes[%i]))" % (j,j)
      h += ";\n"
    h += "  if (stride_c != std::numeric_limits<unsigned int>::max())  strideC%u%s = stride_c;\n" % (lastStrideC-1, indexChars[lastStrideC-1])

    constStride = None
    for i in range(0,lastStrideA):
      idx = problemType["IndexAssignmentsA"][i]
      constStride = checkConstStride(problemType["SetConstStrideA"], idx)
      if constStride != None:
        h += "  unsigned int strideA%u%s = %d; //SetConstStrideA\n" % (i,
          indexChars[problemType["IndexAssignmentsA"][i]],
          constStride)
      else:
        h += "  unsigned int strideA%u%s = 1" % (i, \
            indexChars[problemType["IndexAssignmentsA"][i]])
        for j in range(0, i):
          h += " * ("
          if j == 0:
            h += "(lda != std::numeric_limits<unsigned int>::max()) ? lda : "
          h += "std::max(minStrides[%i], sizes[%i]))" % \
            (problemType["IndexAssignmentsA"][j],
             problemType["IndexAssignmentsA"][j])
        h += ";\n"
    if constStride == None:
      h += "  if (stride_a != std::numeric_limits<unsigned int>::max())  strideA%u%s = stride_a;\n" % (lastStrideA-1, indexChars[problemType["IndexAssignmentsA"][lastStrideA-1]])

    for i in range(0,lastStrideB):
      idx = problemType["IndexAssignmentsB"][i]
      constStride = checkConstStride(problemType["SetConstStrideB"], idx)
      if constStride != None:
        h += "  unsigned int strideB%u%s = %d; //SetConstStrideB\n" % (i,
          indexChars[problemType["IndexAssignmentsB"][i]],
          constStride)
      else:
        h += "  unsigned int strideB%u%s = 1" % (i, \
            indexChars[problemType["IndexAssignmentsB"][i]])
        for j in range(0, i):
          h += " * ("
          if j == 0:
            h += "(ldb != std::numeric_limits<unsigned int>::max()) ? ldb : "
          h+= "std::max(minStrides[%i], sizes[%i]))" % \
            (problemType["IndexAssignmentsB"][j],
             problemType["IndexAssignmentsB"][j])
        h += ";\n"
    h += "  if (stride_b != std::numeric_limits<unsigned int>::max())  strideB%u%s = stride_b;\n" % (lastStrideB-1, indexChars[problemType["IndexAssignmentsB"][lastStrideB-1]])

    for i in range(0, problemType["TotalIndices"]):
      h += "  unsigned int size%s = sizes[%u];\n" % (indexChars[i], i)
    h += "\n"


    # function call
    h += "  // Check assertions,\n"
    assert(not problemType["UseInitialStridesCD"]) # not supported in old client
    firstStride = 0 if problemType["UseInitialStridesAB"] else 1
    lastStrideD = problemType["NumIndicesC"]
    lastStrideC = problemType["NumIndicesC"]
    lastStrideA = len(problemType["IndexAssignmentsA"])
    lastStrideB = len(problemType["IndexAssignmentsB"])
    numSizes = problemType["TotalIndices"]
    h += "  typedef ProblemDims<%u,%u,%u,%u,%u,%u> ProblemDims_%s;\n" \
        % (firstStride, lastStrideD, lastStrideC, lastStrideA, lastStrideB, numSizes, problemType)
    # TODO - this should be initialized somewhere once?
    h += "  static const ProblemType problemType( "
    h += listToInitializer(problemType["IndicesFree"]) + ", "
    h += listToInitializer(problemType["IndicesSummation"]) + ", "
    h += listToInitializer(problemType["IndicesBatch"]) + ', '
    h += listToInitializer(problemType["IndexAssignmentsA"]) + ', '
    h += listToInitializer(problemType["IndexAssignmentsB"])
    h += ");\n"
    # create problem size - TODO could move this up to the caller
    h += "  ProblemDims_%s pdims(" % problemType
    indexChars = globalParameters["IndexChars"]
    for i in range(firstStride,lastStrideD):
      if i != firstStride: h += ", "
      h += "strideD%u%s" % (i, indexChars[i])
    for i in range(firstStride,lastStrideC):
      h += ", strideC%u%s" % (i, indexChars[i])
    for i in range(firstStride,lastStrideA):
      h += ", strideA%u%s" % (i, \
          indexChars[problemType["IndexAssignmentsA"][i]])
    for i in range(firstStride,lastStrideB):
      h += ", strideB%u%s" % (i, \
          indexChars[problemType["IndexAssignmentsB"][i]])
    for i in range(0, problemType["TotalIndices"]):
      h += ", size%s" % indexChars[i]
    h += ");\n"
    h += "  if (!ProblemProperties(pdims,&problemType).validForSolution(solution._assertionRequirements))\n"
    h += "    return tensileStatusAssertFailure;  // problem dims did not meet requirements for solution\n"
    h += "\n"

    h += "  // call solution function\n"
    h += "  TensileSolutionPointer_%s f = reinterpret_cast<TensileSolutionPointer_%s> (solution._functionPtr);\n" \
            % (problemType, problemType)
    if globalParameters["RuntimeLanguage"] == "OCL":
      h += "  return f(solutionLock, static_cast<cl_mem>(deviceD), static_cast<cl_mem>(deviceC), static_cast<cl_mem>(deviceA), static_cast<cl_mem>(deviceB),\n"
    else:
      typeName = dataTypes[0].toCpp()
      destTypeName = destDataTypes[dataType].toCpp()
      computeTypeName = computeDataTypes[dataType].toCpp()
      h += "  return f(solutionLock,\n"
      h += "      static_cast<%s *>(deviceD),\n" % destTypeName
      h += "      static_cast<%s *>(deviceC),\n" % destTypeName
      h += "      static_cast<%s *>(deviceA),\n" % typeName
      h += "      static_cast<%s *>(deviceB),\n" % typeName
    h += "      alpha,\n"
    if problemType["UseBeta"]:
      h += "      beta,\n"
    for i in range(firstStride,lastStrideD):
      h += "      strideD%u%s,\n" % (i, indexChars[i])
    for i in range(firstStride,lastStrideC):
      h += "      strideC%u%s,\n" % (i, indexChars[i])
    for i in range(firstStride,lastStrideA):
      h += "      strideA%u%s,\n" % (i, \
          indexChars[problemType["IndexAssignmentsA"][i]])
    for i in range(firstStride,lastStrideB):
      h += "      strideB%u%s,\n" % (i, \
          indexChars[problemType["IndexAssignmentsB"][i]])
    for i in range(0, problemType["TotalIndices"]):
      h += "      size%s,\n" % indexChars[i]
    h +=   "      stream,\n"
    if globalParameters["RuntimeLanguage"] == "OCL":
       h += "      numEvents, event_wait_list, outputEvent ); // events\n"
    else:
       h += "      numEvents,\n"
       h += "      startEvent,\n"
       h += "      stopEvent,\n"
       h += "      static_cast<float *>(deviceWS)); // events\n"

    h += "};\n"
    h += "\n"
  else:
    ############################################################################
    # Generated Call to Function
    ############################################################################
    for enqueue in [True, False]:
      functionName = "tensile" if enqueue else "tensileGetSolutionName"
      returnName = "TensileStatus" if enqueue else "const char *"
      h += "/* generated call to function */\n"
      h += "template<typename DataType, typename DestDataType, typename ComputeDataType>\n"
      h += "%s generatedCallTo_%s(\n" % (returnName, functionName)
      h += "    unsigned int *sizes,\n"
      h += "    unsigned int *minStrides,\n"
      h += "    ComputeDataType alpha,\n"
      h += "    ComputeDataType beta,\n"
      h += "    unsigned int lda,\n"
      h += "    unsigned int ldb,\n"
      h += "    unsigned int ldc,\n"
      h += "    unsigned int ldd,\n"
      h += "    unsigned int strideA,\n"
      h += "    unsigned int strideB,\n"
      h += "    unsigned int strideC,\n"
      h += "    unsigned int strideD,\n"
      h += "    unsigned int numEvents = 0,\n"

      if globalParameters["RuntimeLanguage"] == "OCL":
        h += "    cl_event *event_wait_list = NULL,\n"
        h += "    cl_event *outputEvent = NULL );\n\n"
      else:
        h += "    hipEvent_t *startEvent = NULL,\n"
        h += "    hipEvent_t *stopEvent = NULL );\n\n"


#need to get DestDataType in here
      for dataType in dataTypes:
        typeName = dataType.toCpp()
        destDataType = destDataTypes[dataType]
        destTypeName = destDataType.toCpp()
        computeDataType = computeDataTypes[dataType]
        computeTypeName = computeDataType.toCpp()
        functionsForDataType = []
        for problemType in problemTypesForDataType[dataType]:
          for scheduleName in schedulesForProblemType[problemType]:
            functionsForDataType.append([scheduleName, problemType])
        h += "template<>\n"
        h += "inline %s generatedCallTo_%s<%s, %s, %s>(\n" \
            % (returnName, functionName, typeName, destTypeName, computeTypeName)
        h += "    unsigned int *sizes,\n"
        h += "    unsigned int *minStrides,\n"
        h += "    %s alpha,\n" % computeTypeName
        h += "    %s beta,\n" % computeTypeName
        h += "    unsigned int lda,\n"
        h += "    unsigned int ldb,\n"
        h += "    unsigned int ldc,\n"
        h += "    unsigned int ldd,\n"
        h += "    unsigned int strideA,\n"
        h += "    unsigned int strideB,\n"
        h += "    unsigned int strideC,\n"
        h += "    unsigned int strideD,\n"
        h += "    unsigned int numEvents, \n"

        if globalParameters["RuntimeLanguage"] == "OCL":
          h += "    cl_event *event_wait_list,\n"
          h += "    cl_event *outputEvent ) {\n\n"
        else:
          h += "    hipEvent_t *startEvent,\n"
          h += "    hipEvent_t *stopEvent ) {\n\n"

        h += "    unsigned int functionIdxForDataType = functionInfo[functionIdx][4];\n"

        for functionIdx in range(0, len(list(functionsForDataType))):
          function = functionsForDataType[functionIdx]
          scheduleName = function[0]
          problemType = function[1]
          if len(list(functionsForDataType))> 1:
            if functionIdx == 0:
              h += "  if (functionIdxForDataType == %u) {\n" % functionIdx
            elif functionIdx == len(list(functionsForDataType))-1:
              h += "  } else {\n"
            else:
              h += "  } else if (functionIdxForDataType == %u) {\n" \
                  % functionIdx

          # strides
          indexChars = globalParameters["IndexChars"]
          firstStride = 1
          assert(not problemType["UseInitialStridesCD"]) # not supported in old client
          if problemType["UseInitialStridesAB"]:
            firstStride = 0
          lastStrideD = problemType["NumIndicesC"]
          lastStrideC = problemType["NumIndicesC"]
          lastStrideA = len(problemType["IndexAssignmentsA"])
          lastStrideB = len(problemType["IndexAssignmentsB"])

          # calculate strides
          for i in range(0,lastStrideD):
            h += "    unsigned int strideD%u%s = 1" % (i, indexChars[i])
            for j in range(0, i):
              h += "*sizes[%i]" % j
            h += ";\n"
          h += "    if (strideD != std::numeric_limits<unsigned int>::max())  strideD%u%s = strideD;\n" % (lastStrideD-1, indexChars[lastStrideD-1])
          for i in range(0,lastStrideC):
            h += "    unsigned int strideC%u%s = 1" % (i, indexChars[i])
            for j in range(0, i):
              h += "*sizes[%i]" % j
            h += ";\n"
          h += "    if (strideC != std::numeric_limits<unsigned int>::max())  strideC%u%s = strideC;\n" % (lastStrideC-1, indexChars[lastStrideC-1])

          for i in range(0,lastStrideA):
            h += "    unsigned int strideA%u%s = 1" % (i, \
                indexChars[problemType["IndexAssignmentsA"][i]])
            for j in range(0, i):
              h += "*sizes[%i]" % \
                problemType["IndexAssignmentsA"][j]
            h += ";\n"
          h += "    if (strideA != std::numeric_limits<unsigned int>::max())  strideA%u%s = strideA;\n" % (lastStrideA-1, indexChars[problemType["IndexAssignmentsA"][lastStrideA-1]])
          for i in range(0,lastStrideB):
            h += "    unsigned int strideB%u%s = 1" % (i, \
                indexChars[problemType["IndexAssignmentsB"][i]])
            for j in range(0, i):
              h += "*sizes[%i]" % \
                problemType["IndexAssignmentsB"][j]
            h += ";\n"
          h += "    if (strideB != std::numeric_limits<unsigned int>::max())  strideB%u%s = strideB;\n" % (lastStrideB-1, indexChars[problemType["IndexAssignmentsB"][lastStrideB-1]])
          for i in range(0, problemType["TotalIndices"]):
            h += "    unsigned int size%s = sizes[%u];\n" % (indexChars[i], i)

          # function call
          h += "    // call solution function\n"
          h += "    return %s_%s(\n" % (functionName, problemType)
          if enqueue:
            if globalParameters["RuntimeLanguage"] == "OCL":
              h += "        static_cast<cl_mem>(deviceD),\n"
              h += "        static_cast<cl_mem>(deviceC),\n"
              h += "        static_cast<cl_mem>(deviceA),\n"
              h += "        static_cast<cl_mem>(deviceB),\n"
            else:
              h += "        static_cast<%s *>(deviceD),\n" % destTypeName
              h += "        static_cast<%s *>(deviceC),\n" % destTypeName
              h += "        static_cast<%s *>(deviceA),\n" % typeName
              h += "        static_cast<%s *>(deviceB),\n" % typeName
            h += "        alpha,\n"
            if problemType["UseBeta"]:
              h += "        beta,\n"
          for i in range(firstStride,lastStrideD):
            h += "        strideD%u%s,\n" % (i, indexChars[i])
          for i in range(firstStride,lastStrideC):
            h += "        strideC%u%s,\n" % (i, indexChars[i])
          for i in range(firstStride,lastStrideA):
            h += "        strideA%u%s,\n" % (i, \
                indexChars[problemType["IndexAssignmentsA"][i]])
          for i in range(firstStride,lastStrideB):
            h += "        strideB%u%s,\n" % (i, \
                indexChars[problemType["IndexAssignmentsB"][i]])
          for i in range(0, problemType["TotalIndices"]):
            h += "        size%s%s\n" % (indexChars[i], "," if i != problemType["TotalIndices"]-1 else "")
          if enqueue:
            if globalParameters["RuntimeLanguage"] == "OCL":
              h += ", stream, numEvents, event_wait_list, outputEvent"
            else:
              h += ", stream, numEvents, startEvent, stopEvent, static_cast<float *>(deviceWS)"
          h += ");\n"

        if len(functionsForDataType) > 1:
          h += "  }\n" # close last if
        h += "};\n" # close callToFunction

  ##############################################################################
  # Results File Name
  ##############################################################################
  if forBenchmark:
    h += "/* results file name */\n"
    resultsFileName = os.path.join(stepBaseDir, \
        "../Data","%s.csv" % stepName)
    resultsFileName = resultsFileName.replace("\\", "\\\\")
    h += "const char *resultsFileName = \"%s\";\n" % resultsFileName

    granularityFileName = os.path.join(stepBaseDir, \
        "../Data","%s_Granularity.csv" % stepName)

    granularityFileName = granularityFileName.replace("\\", "\\\\")
    h += "const char *granularityFileName = \"%s\";\n" % granularityFileName

  ##############################################################################
  # Write File
  ##############################################################################
  clientParametersFile = open(os.path.join(globalParameters["WorkingPath"], \
      "ClientParameters.h"), "w")
  clientParametersFile.write(CHeader)
  clientParametersFile.write(h)
  clientParametersFile.close()
