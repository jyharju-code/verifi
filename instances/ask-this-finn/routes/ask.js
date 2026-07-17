import { Router } from 'express';

export const askRouter = Router();

askRouter.post('/ask', (_req, res) => {
  res.status(501).json({
    error: 'Ask This Finn is not open yet',
    detail: 'This instance is built after Verify API. See verifi.cloud for the Verify API.',
  });
});
