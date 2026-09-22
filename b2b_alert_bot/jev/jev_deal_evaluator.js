#!/usr/bin/env node
/**
 * Jev Probabilistic Deal Evaluator for B2B Telegram Alerts.
 * 
 * Uses typesafe-ai/jev via Vercel AI Gateway to compute calibrated probabilities
 * on incoming leads:
 * 1. is_high_ticket (boolean) - Probability that the contract budget exceeds $2k fixed or $50/hr.
 * 2. budget_tier (choice) - Probabilities across micro_budget, mid_tier, high_ticket, enterprise.
 * 3. client_intent (score) - Hiring intent confidence (1-5 scale).
 */

import fs from 'fs';
import path from 'path';
import { experimental_evaluate as evaluate } from 'ai';

// Ensure AI_GATEWAY_API_KEY is available
if (!process.env.AI_GATEWAY_API_KEY) {
  const envFiles = [
    path.resolve(process.cwd(), '.env'),
    path.resolve(process.env.HOME || '/root', '.env'),
  ];
  for (const file of envFiles) {
    if (fs.existsSync(file)) {
      const content = fs.readFileSync(file, 'utf8');
      for (const line of content.split('\n')) {
        const trimmed = line.trim();
        if (trimmed && !trimmed.startsWith('#') && trimmed.includes('=')) {
          const [k, ...v] = trimmed.split('=');
          const val = v.join('=').trim().replace(/^['"]|['"]$/g, '');
          if (k.trim() === 'AI_GATEWAY_API_KEY') {
            process.env.AI_GATEWAY_API_KEY = val;
            break;
          }
        }
      }
    }
  }
}

async function readInput() {
  if (process.argv[2]) {
    try {
      return JSON.parse(process.argv[2]);
    } catch {
      return { text: process.argv[2] };
    }
  }
  return new Promise((resolve) => {
    let data = '';
    process.stdin.on('data', (chunk) => { data += chunk; });
    process.stdin.on('end', () => {
      try {
        resolve(JSON.parse(data));
      } catch {
        resolve({ text: data.trim() });
      }
    });
  });
}

export async function evaluateDeal(dealData) {
  const context = typeof dealData === 'string' 
    ? dealData 
    : `
TITLE: ${dealData.title || 'Untitled Opportunity'}
BUDGET: ${dealData.budget || dealData.budget_badge || 'Unspecified'}
SCOPE: ${dealData.scope || dealData.scope_bullet || 'No scope details provided'}
SKILLS: ${dealData.skills || dealData.skills_bullet || 'General'}
CLIENT: ${dealData.client || 'Direct Client'}
SOURCE: ${dealData.source || 'Direct Feed'}
`;

  const result = await evaluate({
    model: 'typesafe-ai/jev',
    state: context,
    questions: {
      is_high_ticket: {
        type: 'boolean',
        instructions: 'Is this contract or job a legitimate high-ticket B2B opportunity with a budget of at least $2,000 fixed or $50/hour?'
      },
      budget_tier: {
        type: 'choice',
        instructions: 'What is the estimated budget tier for this contract based on scope and role seniority?',
        criteria: {
          sub_2k: 'Under $2,000 or low-budget commodity freelance task',
          mid_tier: '$2,000 - $10,000 fixed or $50 - $100/hr contract',
          high_ticket: '$10,000 - $30,000 fixed or $100 - $200/hr architecture contract',
          enterprise: '$30,000+ fixed or long-term multi-month enterprise engagement'
        }
      },
      client_intent: {
        type: 'score',
        instructions: 'Rate client hiring urgency and decision intent from 1 (vague or casual query) to 5 (immediate, funded, and urgent hiring need).',
        criteria: ['1', '2', '3', '4', '5']
      }
    }
  });

  const highTicketProb = result.answers.is_high_ticket.probability;
  const isHighTicket = highTicketProb >= 0.65;
  const topTier = result.answers.budget_tier.choice;
  const tierProb = result.answers.budget_tier.probabilities[topTier] || 0.0;
  const intentScore = result.answers.client_intent.score;

  const tierLabels = {
    sub_2k: '<$2,000 (Low Ticket)',
    mid_tier: '$2k - $10k (Mid-Tier)',
    high_ticket: '$10k - $30k (High-Ticket)',
    enterprise: '$30k+ (Enterprise)'
  };

  const badgeSummary = `🛡️ <b>JEV AI VERIFIED:</b> ${(highTicketProb * 100).toFixed(0)}% High-Ticket Prob | 💼 ${tierLabels[topTier] || topTier} (${(tierProb * 100).toFixed(0)}%) | ⚡ Urgency: ${intentScore.toFixed(1)}/5.0`;

  return {
    success: true,
    is_high_ticket: isHighTicket,
    high_ticket_probability: highTicketProb,
    budget_tier: topTier,
    tier_label: tierLabels[topTier] || topTier,
    tier_probability: tierProb,
    all_tier_probabilities: result.answers.budget_tier.probabilities,
    client_intent_score: intentScore,
    badge_summary: badgeSummary,
    usage: result.usage
  };
}

async function main() {
  try {
    const input = await readInput();
    if (!input || (!input.text && !input.title)) {
      console.error(JSON.stringify({ error: 'No deal input provided' }));
      process.exit(1);
    }
    const evaluation = await evaluateDeal(input);
    console.log(JSON.stringify(evaluation, null, 2));
  } catch (err) {
    console.error(JSON.stringify({ error: err.message || String(err) }));
    process.exit(1);
  }
}

if (process.argv[1] && process.argv[1].endsWith('jev_deal_evaluator.js')) {
  main();
}
