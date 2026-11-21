import { describe, expect, it } from "vitest";

import { getBarChartScale } from "./BarChartScale";

describe("getBarChartScale", () => {
  it("places nonnegative bars at the left edge and scales to the maximum", () => {
    expect(getBarChartScale([4, 8])).toEqual({
      minimum: 0,
      maximum: 8,
      range: 8,
      zeroPositionPercent: 0,
      points: [
        { startPercent: 0, widthPercent: 50 },
        { startPercent: 0, widthPercent: 100 },
      ],
    });
  });

  it("positions zero proportionally when values have mixed signs", () => {
    const scale = getBarChartScale([-4, 8]);
    expect(scale).toMatchObject({ minimum: -4, maximum: 8, range: 12 });
    expect(scale.zeroPositionPercent).toBeCloseTo(100 / 3);
    expect(scale.points[0]?.startPercent).toBe(0);
    expect(scale.points[0]?.widthPercent).toBeCloseTo(100 / 3);
    expect(scale.points[1]?.startPercent).toBeCloseTo(100 / 3);
    expect(scale.points[1]?.widthPercent).toBeCloseTo(200 / 3);
  });

  it("places zero at the right edge for all-negative values", () => {
    expect(getBarChartScale([-2, -8])).toEqual({
      minimum: -8,
      maximum: 0,
      range: 8,
      zeroPositionPercent: 100,
      points: [
        { startPercent: 75, widthPercent: 25 },
        { startPercent: 0, widthPercent: 100 },
      ],
    });
  });

  it("keeps zero-width values at the nonnegative origin", () => {
    expect(getBarChartScale([0, 4])).toEqual({
      minimum: 0,
      maximum: 4,
      range: 4,
      zeroPositionPercent: 0,
      points: [
        { startPercent: 0, widthPercent: 0 },
        { startPercent: 0, widthPercent: 100 },
      ],
    });
  });

  it("scales a single point from zero across the track", () => {
    expect(getBarChartScale([6])).toEqual({
      minimum: 0,
      maximum: 6,
      range: 6,
      zeroPositionPercent: 0,
      points: [{ startPercent: 0, widthPercent: 100 }],
    });
  });

  it("keeps a zero-only or empty scale finite", () => {
    expect(getBarChartScale([0, 0])).toEqual({
      minimum: 0,
      maximum: 0,
      range: 0,
      zeroPositionPercent: 0,
      points: [
        { startPercent: 0, widthPercent: 0 },
        { startPercent: 0, widthPercent: 0 },
      ],
    });
    expect(getBarChartScale([])).toEqual({
      minimum: 0,
      maximum: 0,
      range: 0,
      zeroPositionPercent: 0,
      points: [],
    });
  });
});
