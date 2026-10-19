// SPDX-License-Identifier: MIT-0
module.exports = {
  testEnvironment: 'node',
  roots: ['<rootDir>/test'],
  testMatch: ['**/*.test.ts'],
  transform: {
    '^.+\\.tsx?$': 'ts-jest',
  },
  moduleFileExtensions: ['ts', 'tsx', 'js', 'jsx', 'json', 'node'],
  // Cap parallel workers: each synth-heavy worker can hold over 1 GB, and an
  // uncapped run fanned out to 8 workers (about 5 GB) on a shared machine.
  maxWorkers: 2,
};
