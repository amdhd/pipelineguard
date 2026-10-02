import { describe, it, expect } from 'vitest'
import request from 'supertest'
import app from '../src/index'

describe('GET /health', () => {
  it('returns 200 with a healthy status envelope', async () => {
    const res = await request(app).get('/health')
    expect(res.status).toBe(200)
    expect(res.body).toMatchObject({ status: 'healthy' })
    expect(typeof res.body.timestamp).toBe('string')
    expect(res.body).toHaveProperty('version')
    expect(res.body).toHaveProperty('environment')
  })

  it('returns a valid ISO-8601 timestamp', async () => {
    const res = await request(app).get('/health')
    expect(new Date(res.body.timestamp).toISOString()).toBe(res.body.timestamp)
  })
})

describe('unknown routes', () => {
  it('returns 404 for an undefined path', async () => {
    const res = await request(app).get('/does-not-exist')
    expect(res.status).toBe(404)
  })

  it('returns the JSON error envelope, not an HTML page', async () => {
    const res = await request(app).get('/does-not-exist')
    expect(res.headers['content-type']).toMatch(/application\/json/)
    expect(res.body).toEqual({ status: 'error', message: 'Not Found' })
  })
})

describe('response headers', () => {
  it('does not advertise the framework', async () => {
    const res = await request(app).get('/health')
    expect(res.headers['x-powered-by']).toBeUndefined()
  })
})

describe('error handler', () => {
  it('returns 400, not 500, for a malformed JSON body', async () => {
    const res = await request(app)
      .post('/health')
      .set('Content-Type', 'application/json')
      .send('{"broken":')
    expect(res.status).toBe(400)
    expect(res.body).toEqual({ status: 'error', message: 'Bad Request' })
  })
})
