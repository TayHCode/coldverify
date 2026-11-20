import { describe, expect, it } from "vitest";
import { add } from "../calc";

describe("calc", () => {
  it("adds two numbers", () => {
    expect(add(1, 2)).toBe(3);
  });
});
