# HANAS Frontend

React + TypeScript + Vite dashboard for the HANAS nutrient control system.

## Local Development

Install dependencies:

```bash
npm ci
```

Run the dev server:

```bash
npm run dev
```

Build for production:

```bash
npm run build
```

Run quality checks before deployment:

```bash
npm run lint
npm run build
```


Browser tests are in `tests/dashboard.spec.ts`, with test data in `tests/fixtures/`.
Run `npm run build` before `npm run test:e2e`.
