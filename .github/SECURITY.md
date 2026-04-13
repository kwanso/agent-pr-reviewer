# Security Policy

## Reporting Security Vulnerabilities

**⚠️ IMPORTANT: Do NOT open a public GitHub issue to report security vulnerabilities.**

If you discover a security vulnerability in PR Review Agent, please report it to us immediately by email instead.

### How to Report

Send an email to: **security@kwanso.com**

**Include in your report:**

1. **Description** — Clear description of the vulnerability
2. **Location** — Affected file(s) or component(s)
3. **Steps to Reproduce** — How to reproduce the vulnerability
4. **Impact** — What could an attacker do with this vulnerability?
   - Data exposure?
   - Code execution?
   - Denial of service?
   - Authentication bypass?
5. **Proof of Concept** — If you have one (optional but helpful)
6. **Suggested Fix** — If you have one (optional but appreciated)

### Response Process

- **48 hours:** We will acknowledge receipt of your report
- **7 days:** We will provide an initial assessment and timeline
- **30 days:** We aim to release a fix for confirmed vulnerabilities
- **Coordinated Disclosure:** We will work with you on the timeline before public disclosure

### What Happens Next

1. We will investigate the vulnerability
2. If confirmed, we will:
   - Create a fix
   - Develop a security patch
   - Release it as a new version
   - Credit you in the release notes (if you wish)
3. If not confirmed, we will explain why
4. We will keep you updated throughout the process

## Security Best Practices

### For Users

When deploying PR Review Agent:

1. **Environment Variables** — Store credentials in environment variables, not in code
   ```bash
   export GITHUB_APP_ID=your_app_id
   export LLM_API_KEY=your_api_key
   ```

2. **GitHub App Permissions** — Use least-privilege principle
   - Only grant necessary permissions
   - Regularly audit installed apps

3. **Webhooks** — Always use HMAC-SHA256 verification
   - Set a strong `WEBHOOK_SECRET`
   - Verify webhook signatures (we do this automatically)

4. **API Keys** — Rotate regularly
   - Set expiration dates
   - Use dedicated accounts/apps

5. **Network Security** — When deploying:
   - Use HTTPS for webhooks (not HTTP)
   - Restrict access to internal endpoints
   - Use authentication for health/admin endpoints

6. **Dependency Updates** — Keep dependencies up to date
   ```bash
   pip install --upgrade pip
   pip install -e . --upgrade
   ```

### For Developers

1. **No Secrets in Code** — Never commit API keys, tokens, or passwords
2. **Input Validation** — All external inputs are validated with Pydantic
3. **Dependencies** — We use security scanning (bandit) in CI/CD
4. **Code Review** — All changes require review before merge
5. **Testing** — Comprehensive test suite catches regressions

## Known Security Considerations

### 1. GitHub App Private Key
- **Risk:** If exposed, attacker can impersonate the app
- **Mitigation:** Store in `.env`, never commit, use GitHub Secrets in CI/CD

### 2. API Keys
- **Risk:** LLM and Slack keys could be exposed
- **Mitigation:** Store in environment variables, not in code
- **Rotation:** Change keys regularly

### 3. Webhook Signature Verification
- **Protection:** We verify HMAC-SHA256 signatures on all webhooks
- **Best Practice:** Use a strong, randomly generated `WEBHOOK_SECRET`

### 4. Database Access
- **SQLite:** Local file-based, only accessible by the process
- **Checkpoints:** Not encrypted; store in secure directory with restricted permissions

## Security Scanning

We perform automated security scanning:

- **Code Analysis** — Using `bandit` for Python security issues
- **Dependency Checks** — GitHub Dependabot monitors dependencies
- **CI/CD** — Security checks run on every push and PR

View results in the GitHub Actions workflow: `.github/workflows/tests.yml`

## Responsible Disclosure

We follow responsible disclosure practices:

1. **Confidentiality** — We will keep your report confidential until a fix is released
2. **Credit** — We will credit you (unless you prefer anonymity)
3. **Timeline** — We will work transparently with you on release timing
4. **No Legal Action** — We will not pursue legal action against security researchers who follow this policy

## Security Contact

- **Email:** security@kwanso.com
- **Response Time:** 48 hours
- **Language:** English preferred, others welcome

---

## Additional Resources

- [OWASP Top 10](https://owasp.org/www-project-top-ten/) — Common web vulnerabilities
- [GitHub Security Guide](https://docs.github.com/en/code-security) — GitHub security features
- [CWE/SANS Top 25](https://cwe.mitre.org/top25) — Most dangerous software weaknesses

---

**Last Updated:** 2026-04-13
