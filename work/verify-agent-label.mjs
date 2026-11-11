import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
const source=readFileSync('board/server.py','utf8');
function extract(name) {
  const start=source.indexOf('function '+name+'(');
  return source.slice(start,source.indexOf('\n}',start)+2);
}
const html=new Function(`
const CARD={state:'idle'}, CUR='GH-154', BUSY=false, WRITABLE=true;
const PENDING_OPERATIONS=new Map();
${extract('actionsHtml')}
return actionsHtml();`)();
assert.match(html,/data-act='add' data-label='agent'/);
assert.doesNotMatch(html,/textarea|data-open|发布并执行/);
const writes=[];
const run=new Function('postGithubOperation', `
let CUR='GH-154',BUSY=false;const CARD={state:'idle'};
const renderActions=()=>{},toast=()=>{},refreshIssues=async()=>{},refreshCard=()=>{},loadRuns=async()=>{};
const openDialog=()=>{throw new Error('Unexpected confirmation');};
async ${extract('labelAction')}
return labelAction('add','agent');`);
await run(async(url,payload)=>{writes.push({url,payload});return {ok:true};});
assert.deepEqual(writes,[{url:'/api/label',payload:{issue:'GH-154',action:'add',label:'agent',comment:''}}]);
console.log('PASS: direct agent-label action, no form, no confirmation, no comment');
