// SPDX-License-Identifier: MIT-0
import { defineConfig } from '@playwright/test';
export default defineConfig({testDir:'./e2e',webServer:{command:'python3 -m http.server 4173 --bind 127.0.0.1 -d out',port:4173,reuseExistingServer:false},use:{baseURL:'http://127.0.0.1:4173'}});
