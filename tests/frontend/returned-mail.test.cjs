const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('fs'),vm=require('vm');
const ctx={};vm.createContext(ctx);vm.runInContext(fs.readFileSync('frontend/returned-mail.js','utf8'),ctx);
test('folder parser retains leading zeroes and rejects traversal and non-account folders',()=>{
 assert.equal(ctx.ReturnedMail.accountFromPath('Returned Mail/P123456/scan.pdf'),'P123456');
 assert.equal(ctx.ReturnedMail.accountFromPath('000123/scan.jpg'),'000123');
 for(const p of ['file.pdf','../P123/a.pdf','root/not-account/a.pdf','/P123/a.pdf'])assert.throws(()=>ctx.ReturnedMail.accountFromPath(p));
});
test('scan card escapes source text and does not reintroduce cleared forwarding suggestions',()=>{
 const elements={};
 ctx.document={getElementById:id=>elements[id]||(elements[id]={addEventListener(){}})};
 ctx.ReturnedMail.configure({selected:()=>null,identity:()=> 'a'});
 const doc={id:'id',filename:'<img src=x onerror=1>',review_status:'REVIEWED',suggested_forwarding:'STALE OCR',forwarding_address:null,uploaded_at:'2026-11-09',ocr_status:'READ'};
 const html=ctx.ReturnedMail.card(doc);
 assert.ok(!html.includes('<img'));assert.ok(html.includes('&lt;img'));
 assert.ok(!html.includes('STALE OCR'));assert.match(html,/fieldset[^>]*disabled/);
});
test('new OCR forwarding is labeled as a research lead and never postal validated',()=>{
 const html=ctx.ReturnedMail.card({id:'id',review_status:'PENDING',suggested_forwarding:'100 Main St',ocr_status:'READ'});
 assert.match(html,/research lead, not a confirmed correction/);assert.match(html,/100 Main St/);
});
