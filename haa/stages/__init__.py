"""Pipeline stages. Each stage is a single LLM call orchestrated by code.

The pipeline (haa/pipeline.py) owns all control flow — a stage returns a
:class:`StageResult` and never decides the next stage itself.
"""

from haa.stages.analyze import AnalyzeStage
from haa.stages.base import BaseStage, StageContext, StageResult, StageStatus
from haa.stages.design import DesignStage
from haa.stages.exp_feasibility import ExpFeasibilityStage
from haa.stages.exp_spec import ExpSpecStage
from haa.stages.grade import GradeStage
from haa.stages.human_review import HumanReviewStage
from haa.stages.novelty import NoveltyStage
from haa.stages.pilot import PilotStage
from haa.stages.refine import RefineStage
from haa.stages.review import ReviewStage
from haa.stages.screen import ScreenStage
from haa.stages.seek import SeekStage
from haa.stages.verify import VerifyStage
from haa.stages.write import WriteStage

__all__ = [
    # interface types
    "AnalyzeStage",
    "BaseStage",
    "StageContext",
    "StageResult",
    "StageStatus",
    # concrete stages
    "SeekStage",
    "NoveltyStage",
    "ScreenStage",
    "DesignStage",
    "VerifyStage",
    "GradeStage",
    "ExpSpecStage",
    "ExpFeasibilityStage",
    "PilotStage",
    "HumanReviewStage",
    "WriteStage",
    "ReviewStage",
    "RefineStage",
]
