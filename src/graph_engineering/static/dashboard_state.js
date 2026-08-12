function dataSignature(value) {
  return JSON.stringify(value ?? null);
}

export function changedSections(previous, next) {
  return Object.keys(next).filter(
    (section) => dataSignature(previous[section]) !== dataSignature(next[section]),
  );
}
