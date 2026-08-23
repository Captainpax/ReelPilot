"""Confidence-aware least-squares motion estimation over timestamped positions."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MotionSample:
    """Store one timestamped and confidence-scored pixel position."""

    timestamp_seconds: float
    position_pixels: float
    confidence: float


@dataclass(frozen=True, slots=True)
class MotionEstimate:
    """Describe filtered position, velocity, and acceleration in pixel units."""

    position_pixels: float
    velocity_pixels_per_second: float
    acceleration_pixels_per_second_squared: float


class MotionEstimator:
    """Timestamp-aware five-sample estimator with confidence and jump rejection."""

    def __init__(self, *, maximum_samples: int = 5) -> None:
        """Create an estimator retaining at most ``maximum_samples`` observations."""
        self._samples: deque[MotionSample] = deque(maxlen=maximum_samples)
        self._previous_velocity = 0.0

    def reset(self) -> None:
        """Discard motion history and accumulated velocity state."""
        self._samples.clear()
        self._previous_velocity = 0.0

    def update(
        self,
        position_pixels: float,
        timestamp_seconds: float,
        confidence: float,
        bar_length_pixels: int,
    ) -> MotionEstimate:
        """Filter one sample, reject implausible jumps, and return an estimate.

        Examples:
            >>> estimator = MotionEstimator()
            >>> estimator.update(100.0, 0.0, 1.0, 96).position_pixels
            100.0
            >>> estimator.update(104.0, 0.02, 1.0, 96).velocity_pixels_per_second > 0
            True

        """
        if self._samples:
            previous = self._samples[-1]
            elapsed_seconds = max(0.001, timestamp_seconds - previous.timestamp_seconds)
            maximum_jump_pixels = max(
                bar_length_pixels * 1.25,
                bar_length_pixels * 7.0 * elapsed_seconds,
            )
            if (
                confidence < 0.75
                and abs(position_pixels - previous.position_pixels) > maximum_jump_pixels
            ):
                return self.estimate()

        self._samples.append(MotionSample(timestamp_seconds, position_pixels, confidence))
        estimate = self.estimate()
        if len(self._samples) >= 2:
            elapsed_seconds = max(
                0.01,
                self._samples[-1].timestamp_seconds - self._samples[-2].timestamp_seconds,
            )
            acceleration = (
                estimate.velocity_pixels_per_second - self._previous_velocity
            ) / elapsed_seconds
            acceleration = (
                0.35 * acceleration + 0.65 * estimate.acceleration_pixels_per_second_squared
            )
            estimate = MotionEstimate(
                estimate.position_pixels,
                estimate.velocity_pixels_per_second,
                acceleration,
            )
        self._previous_velocity = estimate.velocity_pixels_per_second
        return estimate

    def estimate(self) -> MotionEstimate:
        """Estimate current motion without adding another sample."""
        if not self._samples:
            return MotionEstimate(0.0, 0.0, 0.0)
        if len(self._samples) == 1:
            return MotionEstimate(self._samples[-1].position_pixels, 0.0, 0.0)

        origin_seconds = self._samples[-1].timestamp_seconds
        weighted_times = [sample.timestamp_seconds - origin_seconds for sample in self._samples]
        weights = [max(0.2, sample.confidence) for sample in self._samples]
        weight_sum = sum(weights)
        mean_time = (
            sum(t * w for t, w in zip(weighted_times, weights, strict=True)) / weight_sum
        )
        mean_position = (
            sum(
                sample.position_pixels * weight
                for sample, weight in zip(self._samples, weights, strict=True)
            )
            / weight_sum
        )
        numerator = sum(
            weight * (sample_time - mean_time) * (sample.position_pixels - mean_position)
            for sample, sample_time, weight in zip(
                self._samples, weighted_times, weights, strict=True
            )
        )
        denominator = sum(
            weight * (sample_time - mean_time) ** 2
            for sample_time, weight in zip(weighted_times, weights, strict=True)
        )
        velocity = numerator / denominator if denominator > 1e-9 else 0.0
        predicted_position = mean_position + velocity * (0.0 - mean_time)
        return MotionEstimate(predicted_position, velocity, 0.0)
