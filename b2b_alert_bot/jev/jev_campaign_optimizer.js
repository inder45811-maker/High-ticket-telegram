#!/usr/bin/env node
/**
 * Jev Head-to-Head Campaign Optimizer.
 * 
 * Takes multiple social marketing copy candidates, compares them in a single
 * calibrated evaluation pass with typesafe-ai/jev, and selects the mathematical winner
 * before any post is sent to social media.
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

export async function optimizeCampaign(payload) {
  const { candidates, platform = 'linkedin', deal_info = {} } = payload;
  
  if (!candidates || typeof candidates !== 'object' || Object.keys(candidates).length === 0) {
    throw new Error('Candidates object mapping candidate IDs to text is required');
  }

  const candidateKeys = Object.keys(candidates);
  
  // Format state showing each candidate clearly
  let stateText = `TARGET PLATFORM: ${platform.toUpperCase()}\n`;
  if (deal_info.title) {
    stateText += `CONTRACT: ${deal_info.title} (${deal_info.budget || 'High-Ticket'})\n\n`;
  }

  const criteriaMap = {};
  for (const key of candidateKeys) {
    const text = candidates[key];
    stateText += `=== CANDIDATE [${key}] ===\n${text}\n\n`;
    criteriaMap[key] = `Candidate [${key}] delivers the highest conversion probability and scroll-stopping power`;
  }

  const result = await evaluate({
    model: 'typesafe-ai/jev',
    state: stateText,
    questions: {
      best_candidate: {
        type: 'choice',
        instructions: 'Which candidate has the highest conversion probability to stop the scroll, build authority, and drive clicks to the private Telegram feed on Whop?',
        criteria: criteriaMap
      },
      winning_hook_quality: {
        type: 'score',
        instructions: 'Rate the opening hook strength of the selected winning candidate from 1 (weak/generic) to 5 (irresistible/magnetic).',
        criteria: ['1', '2', '3', '4', '5']
      },
      has_high_spam_risk: {
        type: 'boolean',
        instructions: 'Does the selected winning candidate carry a high risk of algorithmic suppression or being flagged as spam?'
      }
    }
  });

  const bestKey = result.answers.best_candidate.choice;
  const probabilities = result.answers.best_candidate.probabilities || {};
  const winnerProbability = probabilities[bestKey] || 0.0;
  const hookScore = result.answers.winning_hook_quality.score;
  const spamRiskProb = result.answers.has_high_spam_risk.probability;

  // Decision rule: Approved if hook quality >= 2.5 and spam risk <= 0.85
  const isApproved = hookScore >= 2.5 && spamRiskProb <= 0.85;

  return {
    success: true,
    winner_key: bestKey,
    winner_text: candidates[bestKey],
    winner_probability: winnerProbability,
    all_probabilities: probabilities,
    hook_quality_score: hookScore,
    spam_risk_probability: spamRiskProb,
    approved_for_dispatch: isApproved,
    audit_summary: `🏆 JEV CAMPAIGN WINNER: [${bestKey}] (${(winnerProbability * 100).toFixed(0)}% confidence) | Hook: ${hookScore.toFixed(1)}/5.0 | Spam Risk: ${(spamRiskProb * 100).toFixed(0)}%`,
    usage: result.usage
  };
}

async function main() {
  try {
    const input = await readInput();
    const evaluation = await optimizeCampaign(input);
    console.log(JSON.stringify(evaluation, null, 2));
  } catch (err) {
    console.error(JSON.stringify({ error: err.message || String(err) }));
    process.exit(1);
  }
}

if (process.argv[1] && process.argv[1].endsWith('jev_campaign_optimizer.js')) {
  main();
}
