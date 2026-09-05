from dlss5_enabler.operations.contexts import RenoDxContext
from dlss5_enabler.operations.pipeline import PipelineRunner, PipelineStep
from dlss5_enabler.operations.steps import (
    StepConfigureMotionVectors,
    StepConfigureRenoDx,
    StepConfigureWineOverrides,
    StepFetchUpstream,
    StepInjectFeederAndHeaders,
    StepInjectRenoDxAndNgx,
    StepInstallD3D9Translation,
    StepInstallReShade,
    StepInstallVulkanLayer,
    StepMirrorDualLocations,
    StepPrepareRenoDx,
)
from dlss5_enabler.operations.steps_common import StepCleanPreviousInstall, StepSaveRecord, StepValidateTarget


def build_renodx_pipeline() -> PipelineRunner[RenoDxContext]:
    preparation: tuple[PipelineStep[RenoDxContext], ...] = (
        StepValidateTarget(),
        StepConfigureRenoDx(),
        StepFetchUpstream(),
        StepPrepareRenoDx(),
        StepCleanPreviousInstall(),
    )
    installation: tuple[PipelineStep[RenoDxContext], ...] = (
        StepInstallReShade(),
        StepInstallD3D9Translation(),
        StepInjectFeederAndHeaders(),
        StepInjectRenoDxAndNgx(),
        StepConfigureMotionVectors(),
        StepInstallVulkanLayer(),
        StepMirrorDualLocations(),
        StepConfigureWineOverrides(),
    )
    return PipelineRunner((*preparation, *installation, StepSaveRecord()), name="RenoDX Installation Pipeline")
