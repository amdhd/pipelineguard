import { Router } from 'express'

export const healthRouter = Router()

healthRouter.get('/', (_req, res) => {
  res.json({
    status: 'healthy',
    timestamp: new Date().toISOString(),
    version: process.env.APP_VERSION ?? 'unknown',
    environment: process.env.NODE_ENV ?? 'unknown'
  })
})
