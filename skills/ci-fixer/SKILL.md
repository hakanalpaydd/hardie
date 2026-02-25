# CI Fixer Skill

Automatically diagnose and fix CI failures in pull requests.

## Prerequisites

- `BUILDKITE_API_TOKEN` environment variable set
- Access to `bk` or `bklog` CLI tools
- Access to `gh` CLI (GitHub CLI)

## Related Skills

- **buildkite-cli**: Use this skill to fetch and analyze Buildkite logs

## Workflow

### 1. Identify Failed Checks

```bash
# Get all failed checks for a PR
gh pr checks <pr-number> --json name,state,bucket,link | jq '.[] | select(.bucket=="fail")'
```

### 2. Fetch Error Logs

For each failed check with a Buildkite URL:

```bash
# Parse the URL to extract: org, pipeline, build_number, job_id
# URL format: https://buildkite.com/{org}/{pipeline}/builds/{build}#{job_id}

# Fetch and search for errors
bklog parse -org <org> -pipeline <pipeline> -build <build> -job <job-id> -parquet /tmp/build.parquet
bklog query -file /tmp/build.parquet -op search -pattern "FAILURE:|Error:|Type error:" -C 15
```

### 3. Analyze the Error

Common error types and fixes:

| Error Type | Pattern | Typical Fix |
|------------|---------|-------------|
| TypeScript type error | `Type error: Type 'X' is not assignable to type 'Y'` | Update type definitions or cast |
| Missing export | `Module '"X"' has no exported member 'Y'` | Add export to source file |
| Lint error | `error: ...` with rule name | Fix code style issue |
| Test failure | `FAIL src/...` | Fix test or implementation |
| Build error | `Failed to compile` | Fix syntax or import issues |

### 4. Apply the Fix

1. Make the necessary code changes
2. Run local validation if possible
3. Commit with descriptive message

```bash
git add -A
av commit -m "fix: address CI failure - <description>"
```

### 5. Restack and Push

```bash
av restack
av sync --push
```

## Example: Fix a Type Error

**Error from logs:**
```
Type error: Type '"BROWSER_REQUIRED"' is not assignable to type 'WebMCPErrorCode'.
./src/webmcp/tools/searchRestaurants.ts:181:11
```

**Analysis:**
- The string `'BROWSER_REQUIRED'` is not in the `WebMCPErrorCode` union type
- Need to either add it to the type definition or use an existing code

**Fix options:**
1. Add `'BROWSER_REQUIRED'` to `WebMCPErrorCode` type definition
2. Use an existing error code from `WebMCPErrorCode`

**Implementation:**
```typescript
// If adding to type (in types file):
export type WebMCPErrorCode = 'UNKNOWN_ERROR' | 'BROWSER_REQUIRED' | ...;

// Or use existing code (in searchRestaurants.ts):
code: 'UNKNOWN_ERROR',  // Use existing code instead
```

## Common DoorDash/web-next Patterns

- **Rush CI failures**: Usually TypeScript or lint errors
- **Pre-CI Checks**: Often missing dependencies or lockfile issues
- **Knip failures**: Unused exports or dependencies (soft failure)
- **Test failures**: Look for `FAIL` in test output

## Tips

1. **Fix bottom-up**: In stacked PRs, fix the lowest PR first
2. **One issue at a time**: Fix one error, push, wait for CI
3. **Check locally**: Run `rush build` or `npm run build` locally before pushing
4. **Read full context**: The error often shows the exact line and issue

