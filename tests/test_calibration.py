from reelpilot.automation.calibration import BarCalibrator


def test_calibration_requires_five_agreeing_samples() -> None:
    calibrator = BarCalibrator()
    for value in (95, 97, 96, 140):
        assert calibrator.observe(value) is None
    assert calibrator.observe(96) is None
    assert calibrator.observe(96) == 96


def test_calibration_rejects_impossible_lengths_and_resets() -> None:
    calibrator = BarCalibrator()
    for value in (10, 250, None):
        assert calibrator.observe(value) is None
    for value in (72, 72, 73, 71, 72):
        calibrator.observe(value)
    assert calibrator.length_pixels == 72
    calibrator.reset()
    assert calibrator.length_pixels is None
