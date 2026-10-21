# Contributing to XTMEM

Thank you for your interest in contributing to XTMEM! We welcome bug reports, pull requests, documentation improvements, and architectural reviews.

## Code of Conduct

Please be respectful, constructive, and collaborative. Treat fellow maintainers and contributors with kindness and professional courtesy.

## Development Workflow

### 1. Environment Setup

XTMEM requires **Python 3.9+**.

```bash
git clone https://github.com/RyuuzakiLu2023/xtmem.git
cd xtmem

# Create a clean virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install package in editable mode with development dependencies
pip install -e ".[dev]"
```

### 2. Running Tests

All changes must pass existing unit tests and safety guards before being merged:

```bash
# Run full test suite with test isolation
pytest tests/ -v

# Run smoke tests
python tests/test_smoke.py
```

### 3. Sanitization & Pre-push Checklist

To protect multi-agent privacy and prevent environment pollution:

- **No Absolute Paths**: Never commit local absolute paths (e.g. `/home/user/...` or Windows root paths). Use environment variables or relative paths.
- **No Secret Leaks**: Never commit `.env`, `*.db`, `*.sqlite`, private keys, or API tokens.
- **Run the Sanitization Scan**:
  ```bash
  python scripts/scan_drive_letters.py
  ```
- **Commit Messages**: Follow standard conventional commits: `feat: ...`, `fix: ...`, `docs: ...`, `test: ...`, `refactor: ...`.

## Pull Request Guidelines

1. Fork the repository and create your branch from `master`.
2. Add comprehensive tests for any new features or bug fixes.
3. Ensure all tests pass and documentation is updated.
4. Submit a Pull Request with a clear description of the problem solved.
