import express from 'express'
import { healthRouter } from './routes/health'
import { errorHandler } from './middleware/errorHandler'

const app = express()
const PORT = process.env.PORT || 3000

app.use(express.json())
app.use('/health', healthRouter)
app.use(errorHandler)

/* istanbul ignore next -- server bootstrap is not exercised in unit tests */
if (process.env.NODE_ENV !== 'test') {
  app.listen(PORT, () => {
    console.log(`PipelineGuard demo app running on port ${PORT}`)
  })
}

export default app
