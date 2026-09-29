import { Request, Response, NextFunction } from 'express'

/**
 * Centralised Express error handler.
 * Logs the error and returns a safe JSON envelope — never leaks stack traces to clients.
 *
 * A 4xx that express/body-parser already set (malformed JSON is a 400) is kept:
 * reporting the client's mistake as a 500 tells the caller the server broke.
 */
export function errorHandler(
  err: Error & { status?: number },
  _req: Request,
  res: Response,
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  _next: NextFunction
): void {
  const status = err.status && err.status >= 400 && err.status < 500 ? err.status : 500
  if (status === 500) {
    console.error('Unhandled error:', err.message)
  }
  res.status(status).json({
    status: 'error',
    message: status === 500 ? 'Internal Server Error' : 'Bad Request'
  })
}
