export function parseBooleanEnv(value, defaultValue = false) {
  if (value === undefined || value === null || value === '') {
    return defaultValue;
  }
  return ['1', 'true', 'yes', 'on'].includes(String(value).trim().toLowerCase());
}

export function shouldForwardInboundMessage({
  isGroup,
  fromMe,
  whatsappMode,
  senderId,
  allowedUsers,
  sessionDir,
  matchesAllowedUser,
}) {
  if (isGroup) {
    // Group messages are forwarded to Python as ambient history even when
    // they are not allowed to invoke the agent. Python still applies
    // group_policy/require_mention before responding.
    return !(fromMe && whatsappMode === 'bot');
  }

  if (fromMe) {
    return true;
  }

  if (whatsappMode === 'self-chat') {
    return false;
  }

  return matchesAllowedUser(senderId, allowedUsers, sessionDir);
}
