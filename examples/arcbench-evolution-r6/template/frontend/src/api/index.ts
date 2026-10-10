import axios from 'axios';

const apiClient = axios.create({
  baseURL: '/api',
  timeout: 5000,
  headers: { 'Content-Type': 'application/json' },
});

export default apiClient;
