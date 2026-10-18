export function mountStandby(options: {target: HTMLElement; scope?: 'personal' | 'orchestration'}): {
  enter(): void;
  destroy(): void;
};
