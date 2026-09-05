import subprocess

import pytest
from pytest_mock import MockerFixture

from dlss5_enabler.platform import NvidiaGpuGeneration, NvidiaGpuInfo, detect_nvidia_gpu_generation


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("NVIDIA GeForce RTX 4090", NvidiaGpuGeneration.RTX40),
        ("NVIDIA GeForce RTX 4070 Ti SUPER", NvidiaGpuGeneration.RTX40),
        ("NVIDIA GeForce RTX 4090 Laptop GPU", NvidiaGpuGeneration.RTX40),
        ("NVIDIA RTX 4000 SFF Ada Generation", NvidiaGpuGeneration.RTX40),
        ("NVIDIA GeForce RTX 5090", NvidiaGpuGeneration.RTX50),
        ("NVIDIA GeForce RTX 5080 Laptop GPU", NvidiaGpuGeneration.RTX50),
        ("NVIDIA RTX PRO 6000 Blackwell Workstation Edition", NvidiaGpuGeneration.RTX50),
        ("NVIDIA GeForce RTX 3090", NvidiaGpuGeneration.OLDER),
        ("NVIDIA GeForce GTX 1080 Ti", NvidiaGpuGeneration.OLDER),
        ("NVIDIA RTX 5000 Ada Generation", NvidiaGpuGeneration.RTX40),
        ("Unexpected GPU", NvidiaGpuGeneration.UNKNOWN),
    ],
)
def test_detect_nvidia_gpu_generation(
    name: str,
    expected: NvidiaGpuGeneration,
    mocker: MockerFixture,
) -> None:
    result = subprocess.CompletedProcess(args=["nvidia-smi"], returncode=0, stdout=f"{name}\n", stderr="")
    run = mocker.patch("dlss5_enabler.platform.gpu.subprocess.run", return_value=result)

    assert detect_nvidia_gpu_generation() == NvidiaGpuInfo(name=name, generation=expected)
    run.assert_called_once_with(
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits"],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=3.0,
    )


def test_detect_nvidia_gpu_generation_accepts_matching_multiple_gpus(mocker: MockerFixture) -> None:
    result = subprocess.CompletedProcess(
        args=["nvidia-smi"],
        returncode=0,
        stdout='"NVIDIA GeForce RTX 4090"\nNVIDIA GeForce RTX 4070 Laptop GPU\n',
        stderr="",
    )
    mocker.patch("dlss5_enabler.platform.gpu.subprocess.run", return_value=result)

    assert detect_nvidia_gpu_generation() == NvidiaGpuInfo(
        name="NVIDIA GeForce RTX 4090; NVIDIA GeForce RTX 4070 Laptop GPU",
        generation=NvidiaGpuGeneration.RTX40,
    )


@pytest.mark.parametrize(
    "stdout",
    [
        "NVIDIA GeForce RTX 4090\nNVIDIA GeForce RTX 5090\n",
        "NVIDIA GeForce RTX 4090\nNVIDIA GeForce RTX 3090\n",
        "",
        "\n\n",
    ],
)
def test_detect_nvidia_gpu_generation_is_conservative_for_ambiguous_output(
    stdout: str,
    mocker: MockerFixture,
) -> None:
    result = subprocess.CompletedProcess(args=["nvidia-smi"], returncode=0, stdout=stdout, stderr="")
    mocker.patch("dlss5_enabler.platform.gpu.subprocess.run", return_value=result)

    info = detect_nvidia_gpu_generation()
    assert info.generation is NvidiaGpuGeneration.UNKNOWN
    assert info.name == ("; ".join(line for line in stdout.splitlines() if line) or None)


def test_detect_nvidia_gpu_generation_handles_command_failure(mocker: MockerFixture) -> None:
    result = subprocess.CompletedProcess(
        args=["nvidia-smi"],
        returncode=1,
        stdout="NVIDIA GeForce RTX 4090\n",
        stderr="error",
    )
    mocker.patch("dlss5_enabler.platform.gpu.subprocess.run", return_value=result)

    assert detect_nvidia_gpu_generation() == NvidiaGpuInfo(name=None, generation=NvidiaGpuGeneration.UNKNOWN)


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=3.0)])
def test_detect_nvidia_gpu_generation_handles_unavailable_command(
    error: OSError | subprocess.TimeoutExpired,
    mocker: MockerFixture,
) -> None:
    mocker.patch("dlss5_enabler.platform.gpu.subprocess.run", side_effect=error)

    assert detect_nvidia_gpu_generation() == NvidiaGpuInfo(name=None, generation=NvidiaGpuGeneration.UNKNOWN)
