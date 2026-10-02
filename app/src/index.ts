import express from 'express'
import { healthRouter } from './routes/health'
import { errorHandler } from './middleware/errorHandler'

const app = express()
const PORT = process.env.PORT || 3000

// Don't advertise the framework in every response.
app.disable('x-powered-by')

app.use(express.json())
app.use('/health', healthRouter)
// Same JSON envelope as errorHandler, instead of express's default HTML page.
app.use((_req, res) => {
  res.status(404).json({ status: 'error', message: 'Not Found' })
})
app.use(errorHandler)

/* istanbul ignore next -- server bootstrap is not exercised in unit tests */
if (process.env.NODE_ENV !== 'test') {
  app.listen(PORT, () => {
    console.log(`PipelineGuard demo app running on port ${PORT}`)
  })
}

export default app
