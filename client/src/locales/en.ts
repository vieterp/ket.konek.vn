/**
 * English UI strings (FR-NFR-034).
 *
 * `Record<TranslationKey, string>` is the whole safety net: a key added to
 * `vi.ts` and forgotten here fails `tsc`, so the release cannot ship a screen
 * that silently falls back to a raw key.
 *
 * Accounting terms follow the wording used in the English column of the chart
 * of accounts (`name_en`), not a literal translation.
 */

import type { TranslationKey } from './vi'

export const en: Record<TranslationKey, string> = {
  'common.appName': 'Konek Két',
  'common.tagline': 'Accounting software',
  'common.loading': 'Loading…',
  'common.retry': 'Retry',
  'common.cancel': 'Cancel',
  'common.close': 'Close',
  'common.signOut': 'Sign out',
  'common.language': 'Language',
  'common.vietnamese': 'Tiếng Việt',
  'common.english': 'English',
  'common.version': 'Version {version}',
  'common.required': 'Required',

  'login.title': 'Sign in',
  'login.subtitle': 'Use the account you were given to open the company books.',
  'login.username': 'Username',
  'login.password': 'Password',
  'login.totpCode': 'Two-factor code',
  'login.totpHint': 'Open your authenticator app and enter the 6 digits shown.',
  'login.submit': 'Sign in',
  'login.submitting': 'Signing in…',
  'login.serverLabel': 'Server',

  'passwordChange.title': 'Change password',
  'passwordChange.intro':
    'This account is on a temporary password. Set a new one before continuing.',
  'passwordChange.currentPassword': 'Current password',
  'passwordChange.newPassword': 'New password',
  'passwordChange.confirmPassword': 'Repeat new password',
  'passwordChange.mismatch': 'The two new passwords do not match.',
  'passwordChange.submit': 'Change password',
  'passwordChange.otherSessionsWarning':
    'Every other session of this account will be signed out afterwards.',

  'totp.title': 'Set up two-factor authentication',
  'totp.intro':
    'This account holds a role that requires two-factor authentication. Register a device, then sign in again.',
  'totp.password': 'Re-enter your password to start',
  'totp.begin': 'Start setup',
  'totp.scan': 'Scan the QR code with an authenticator app.',
  'totp.cantScan': 'Cannot scan? Type this secret into the app instead:',
  'totp.code': 'The 6-digit code shown now',
  'totp.confirm': 'Confirm device',
  'totp.confirmed': 'Device registered. Sign in again and enter a code.',
  'totp.qrAlt': 'QR code for registering the authenticator device',

  'update.title': 'Update required',
  'update.body':
    'This workstation runs an older build than the server accepts. Reading the books still works, but every save is refused until the app is updated.',
  'update.currentVersion': 'Installed: {version}',
  'update.requiredVersion': 'Minimum: {version}',
  'update.howTo': 'The app updates itself on restart. If it does not, tell your administrator.',
  'update.continueReadOnly': 'Continue read-only',

  'handshake.serverBehind':
    'This workstation ({client}) is newer than the server ({server}). Work continues, but the server should be updated.',
  'handshake.failed': 'Cannot reach the server at {url}.',

  'dataset.title': 'Choose a company',
  'dataset.intro': 'Each company is an independent set of books. Pick the one to work in.',
  'dataset.empty': 'This account has no company assigned. Contact your administrator.',
  'dataset.switch': 'Switch company',

  'nav.home': 'Overview',
  'nav.tien-vao-tien-ra': 'Cash in / cash out',
  'nav.mua-hang': 'Purchases',
  'nav.ban-hang': 'Sales',
  'nav.hoa-don-dien-tu': 'E-invoices',
  'nav.kho': 'Inventory',
  'nav.tai-san': 'Assets',
  'nav.luong': 'Payroll',
  'nav.so-sach-thue': 'Ledgers & Tax',
  'nav.danh-muc-thiet-lap': 'Master data & Settings',

  'theme.label': 'Appearance',
  'theme.system': 'Follow system',
  'theme.light': 'Light',
  'theme.dark': 'Dark',

  'status.branch': 'Branch',
  'status.allBranches': 'All assigned branches',
  'status.noBranch': 'No branch assigned',
  'status.readOnly': 'Read-only mode',
  'status.dataset': 'Company',

  'placeholder.title': '{group}',
  'placeholder.body': 'Screens for this group belong to a later phase of the plan.',

  'error.auth.invalid_credentials': 'Wrong username or password.',
  'error.auth.account_locked': 'The account is locked after repeated failures. Try again shortly.',
  'error.auth.throttled': 'Too many attempts. Wait a moment and try again.',
  'error.auth.totp_code_invalid': 'That two-factor code is not valid.',
  'error.auth.totp_code_reused': 'That code was just used. Wait for the next one.',
  'error.auth.password_too_weak': 'The new password does not meet the strength policy.',
  'error.auth.not_authenticated': 'The session expired. Sign in again.',
  'error.auth.permission_denied': 'This account is not allowed to do that.',
  'error.dataset.access_denied': 'This account has no role in that company.',
  'error.system.client_version_unsupported': 'This client build is too old to save data.',
  'error.system.app_key_unavailable':
    'The server has no encryption key configured. Tell your administrator.',
  'error.request.validation_failed': 'Some fields are not valid yet.',
  'error.transport.unexpected_response': 'The server replied with something unreadable.',
  'error.transport.unreachable': 'Cannot reach the server.',
  'error.unknown': 'Something went wrong ({code}). Give the reference id to support.',
}
