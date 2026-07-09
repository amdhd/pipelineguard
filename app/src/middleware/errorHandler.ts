import { Request, Response, NextFunction } from 'express'

/**
 * Centralised Express error handler.
 * Logs the error and returns a safe JSON envelope — never leaks stack traces to clients.
 */
export function errorHandler(
  err: Error,
  _req: Request,
  res: Response,
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  _next: NextFunction
): void {
  console.error('Unhandled error:', err.message)
  res.status(500).json({
    status: 'error',
    message: 'Internal Server Error'
  })
}
