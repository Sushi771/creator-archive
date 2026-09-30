// Playwright tool snippet, not a standalone Node program.
// The external driver sets page.caExpected from the private pending.json ticket.
// Existing browser session and author page must already match the checkpoint.
async (page) => {
  throw new Error("XHS browser automation disabled by account safety policy 2026-09-30");
  const expected = page.caExpected;
  if (!expected || !/^[0-9a-f]{24}$/.test(expected.authorId) ||
      !/^[0-9a-f]{24}$/.test(expected.requestCursor)) {
    throw new Error('A noninitial private checkpoint is required');
  }
  const current = await page.evaluate(() => {
    const query = window.__INITIAL_STATE__?.user?.noteQueries?.value?.[0];
    return {authorId: query?.userId, cursor: query?.cursor};
  });
  if (current.authorId !== expected.authorId || current.cursor !== expected.requestCursor) {
    throw new Error('Browser state differs from durable checkpoint; do not advance');
  }
  const next = page.waitForResponse(r =>
    /^https:\/\/edith\.xiaohongshu\.com\/api\/sns\/web\/v1\/user_posted\?/.test(r.url()),
    {timeout: 10000});
  await page.mouse.wheel(0, 8500);
  const response = await next;
  const payload = await response.json();
  const param = key => decodeURIComponent(
    (response.url().match(new RegExp('[?&]' + key + '=([^&]*)')) || [])[1] || '');
  if (param('user_id') !== expected.authorId || param('cursor') !== expected.requestCursor) {
    throw new Error('Observed request differs from durable checkpoint');
  }
  // Await this response directly. A passive listener may still contain an old page.
  return {
    source: 'observed_api_response', ticket: expected.ticket,
    capturedAt: new Date().toISOString(), authorId: param('user_id'),
    requestCursor: param('cursor'), httpStatus: response.status(),
    success: payload.success, code: payload.code,
    data: {cursor: payload.data?.cursor, has_more: payload.data?.has_more,
      notes: payload.data?.notes?.map(n =>
        ({note_id: n.note_id, user: {user_id: n.user?.user_id}}))}
  };
}
