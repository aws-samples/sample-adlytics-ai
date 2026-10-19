// SPDX-License-Identifier: MIT-0
import nextCoreWebVitals from 'eslint-config-next/core-web-vitals';
import nextTypeScript from 'eslint-config-next/typescript';

// eslint-plugin-react in the pinned Next config does not yet implement ESLint 10's
// rule context API. Keep Next, hooks, accessibility, and TypeScript checks while
// omitting only the incompatible legacy react/* rules.
const compatibleNext = nextCoreWebVitals.map((config) => {
  const { rules, ...rest } = config;
  return rules === undefined
    ? rest
    : {
        ...rest,
        rules: Object.fromEntries(
          Object.entries(rules).filter(([name]) => !name.startsWith('react/')),
        ),
      };
});

const layerRestrictions = ({ aliases, relativeLayers, message }) => [
  'error',
  {
    patterns: [
      { group: aliases, message },
      {
        regex: String.raw`^(?:\.\./)+(?:${relativeLayers.join('|')})(?:/|$)`,
        message,
      },
    ],
  },
];

const config = [
  ...compatibleNext,
  ...nextTypeScript,
  {
    ignores: ['.next/**', '.test-dist/**', 'out/**'],
  },
  {
    // Model-authored text is never pattern-matched for markup or links; rendering every string
    // as an inert text node is the boundary, so raw-HTML rendering is structurally forbidden.
    files: ['app/**/*.{ts,tsx}', 'components/**/*.{ts,tsx}', 'features/**/*.{ts,tsx}', 'lib/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-syntax': [
        'error',
        {
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message:
            'Render text as React text nodes; raw-HTML rendering is forbidden in application code (ADR-082).',
        },
        {
          selector: "Property[key.name='dangerouslySetInnerHTML']",
          message:
            'Render text as React text nodes; raw-HTML rendering is forbidden in application code (ADR-082).',
        },
        {
          selector: "AssignmentExpression > MemberExpression.left[property.name=/^(innerHTML|outerHTML)$/]",
          message:
            'Render text as React text nodes; raw-HTML rendering is forbidden in application code (ADR-082).',
        },
        {
          selector: "CallExpression[callee.property.name='insertAdjacentHTML'], CallExpression[callee.object.name='document'][callee.property.name=/^(write|writeln)$/]",
          message:
            'Render text as React text nodes; raw-HTML rendering is forbidden in application code (ADR-082).',
        },
      ],
    },
  },
  {
    files: ['app/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': layerRestrictions({
        aliases: ['@/components/**', '@/lib/**'],
        relativeLayers: ['components', 'lib'],
        message: 'App routes import features only.',
      }),
    },
  },
  {
    files: ['features/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': layerRestrictions({
        aliases: ['@/app/**', '@/lib/**'],
        relativeLayers: ['app', 'lib'],
        message: 'Features import components only.',
      }),
    },
  },
  {
    files: ['components/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': layerRestrictions({
        aliases: ['@/app/**', '@/features/**'],
        relativeLayers: ['app', 'features'],
        message: 'Components import only components and lib.',
      }),
    },
  },
  {
    files: ['lib/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': layerRestrictions({
        aliases: ['@/app/**', '@/components/**', '@/features/**'],
        relativeLayers: ['app', 'components', 'features'],
        message: 'Lib cannot import an outer layer.',
      }),
    },
  },
];

export default config;
