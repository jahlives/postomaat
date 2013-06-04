
import os
import time
import re
import logging
from postomaat.shared import *

lg=logging.getLogger('postomaat.plugins.recipientrules')

class RulePart(object):
    def __init__(self):
        self.field=None
        self.operator=None
        self.value=''
        

class RecRule(object):
    def __init__(self):
        self.parts=[]
        self.action=None
        self.message=None
        
    def hit(self,suspect):
        """iterates over parts and returns True if all parts match"""
        allvals=suspect.values
        allvals['from_address']=suspect.from_address
        allvals['to_address']=suspect.to_address
        allvals['from_domain']=suspect.from_domain
        allvals['to_domain']=suspect.to_domain
        
        numeric_values=['recipient_count','size','encryption_keysize']
        for nv in numeric_values:
            try:
                allvals[nv]=int(allvals[nv])
            except:
                pass

        #lg.debug(allvals)
        
        for part in self.parts:
            susval=allvals.get(part.field,'')
            try:
                susval=susval.strip()
            except:
                pass
            
            checkval=part.value
            if part.field in numeric_values:
                try:
                    checkval=int(checkval)
                except:
                    pass
            if part.operator=='=':
                hit=susval==checkval
            elif part.operator=='!':
                hit=susval!=checkval
            elif part.operator=='>':
                if susval=='':
                    susval=0
                hit=susval>checkval
            elif part.operator=='<':
                if susval=='':
                    susval=0
                hit=susval<checkval
            elif part.operator=='~':
                hit=re.match(checkval,str(susval))!=None
            else:
                lg.warn("Unknown rule operator '%s'"%part.operator)
                continue
            
            dbgcheckval=checkval
            if hasattr(checkval,'pattern'):
                dbgcheckval=checkval.pattern
            lg.debug(" '%s' %s '%s' ? : %s"%(susval,part.operator,dbgcheckval,hit))
            
            if not hit:
                return None
                
        
        return True
                    

class RecipientRules(ScannerPlugin):
    """
    Rule file format:
    
    [<recipient>]
    <field><operator><value> [<field><operator><value> ...] <ACTION> <message>
    ...
    
    [<recipient>]
    ...
    
    
    <recipient>: email adress, domain or "global"
    <field>: keys as defined in http://www.postfix.org/SMTPD_POLICY_README.html + from_domain,to_domain,from_address,to_address
    <operator>:
        = : equals
        ! : does not equal
        < : smaller than
        > : greater than
        ~ : match regex
    <value> : value to compare the field to using the operator. special value <> means empty
    <ACTION>: the usual postfix actions like REJECT,DEFER,DUNNO,...
    <message>: any message that should be returned to the client. supports template variables
    
    example:
    
    [recipient@example.net]
    from_address=<> REJECT too many bounces to this recipient
    
    [example.org]
    size>20000 from_address~newsletter@ REJECT size ${size} exceeds maximum allowed newsletter size to ${to_address}.    
    from_domain~(somebank.com|someotherbank.com)$ encryption_keysize<512 REJECT we require strong encryption from ${from_domain}
    
    """
    def __init__(self,config,section=None):
        ScannerPlugin.__init__(self,config,section)
        self.logger=self._logger()
        self.requiredvars={
            'configfile':{
                'default':'/etc/postomaat/recipient_rules.conf',
                'description':'Recipient rule file',
            }                  
        }
        self.ruledict=None
        self.lastreload=0

    def filechanged(self):
        filename=self.config.get(self.section,'configfile')
        statinfo=os.stat(filename)
        ctime=statinfo.st_ctime
        if ctime>self.lastreload:
            return True
        return False

    def reload_if_necessary(self):
        if self.ruledict==None or self.filechanged():
            filename=self.config.get(self.section,'configfile')
            lg.info("Reloading file: %s"%filename)
            self.ruledict=self.load_file(filename)
            self.lastreload=time.time()
            
    def examine(self,suspect):
        starttime=time.time()
        retaction=DUNNO
        retmessage=None
        self.reload_if_necessary()
        
        to_domain=suspect.to_domain
        to_address=suspect.to_address
        
        for rec in [to_address,to_domain,'global']:
            if rec in self.ruledict:
                lg.debug("Found rules for %s"%rec)
                for recrule in self.ruledict[rec]:
                    result=recrule.hit(suspect)
                    if result:
                        return recrule.action,apply_template(recrule.message,suspect)
        
        endtime=time.time()
        difftime=endtime-starttime
        suspect.tags['RecipientRules.time']="%.4f"%difftime
        return retaction,retmessage
    
    def load_file(self,filename):
        """returns dict, key=recipient, val=list of RecRule"""
        
        actions=[REJECT,DEFER,ACCEPT,OK,DUNNO,DISCARD,HOLD,PREPEND,REDIRECT,WARN]
        rg="|".join(actions)
        rulepattern=re.compile(r'^(?P<rules>.+)\s(?P<action>'+rg+')\s(?P<message>.+)$',re.IGNORECASE)
        headerpattern=re.compile(r'^\[[a-zA-Z0-9_@+.-]+\]$')
        
        operators=['=','!','>','<','~']
        olist="".join(operators)
        reg='(?P<fieldname>[^'+olist+']+)(?P<operator>['+olist+'])(?P<value>.+)'
        singlerulepattern=re.compile(reg)
        
        retdict={}
        
        lc=0
        
        
        #set to None to disable global rules
        currentrecipient='global'
        retdict['global']=[]
        
        for line in open(filename,'r').readlines():
            lc=lc+1
            line=line.strip()
            if line.startswith('#') or line=='':
                continue
            
            if re.match(headerpattern, line)!=None:
                currentrecipient=line[1:-1]
                if currentrecipient not in retdict:
                    retdict[currentrecipient]=[]
                continue
            
            #lg.debug("Line: %s"%line)
            
            m=re.match(rulepattern,line)
            if m!=None:
                #if we ever don't want global rules anymore
                if currentrecipient==None:
                    lg.warn("%s line %s: not in a recipient section, ignoring: %s"%(filename,lc,line))
                    continue
                
                gd=m.groupdict()
                rulepart=gd['rules']
                action=gd['action'].lower()
                message=gd['message']
                #lg.debug("action=%s message=%s"%(action,message))
                
                
                recrule=RecRule()
                recrule.action=action
                recrule.message=message
                
                problem=False
                for rule in rulepart.split():
                    m=re.match(singlerulepattern, rule)
                    if m==None:
                        lg.warn("%s line %s: can not parse rule '%s'"%(filename,lc,rule))
                        problem=True
                        break
                    
                    gd=m.groupdict()
                    fieldname=gd['fieldname']
                    operator=gd['operator']
                    value=gd['value']
                    if value=='<>':
                        value=''
                    
                    #re precompile
                    if operator=='~':
                        try:
                            value=re.compile(value)
                        except Exception,e:
                            lg.warn("%s line %s: invalid regex '%s' : %s"%(filename,lc,value,str(e)))
                            problem=True
                            break
                    rp=RulePart()
                    rp.field=fieldname
                    rp.operator=operator
                    rp.value=value
                    
                    recrule.parts.append(rp)
                
                if not problem:
                    retdict[currentrecipient].append(recrule)
                #lg.debug("Added rule for %s"%currentrecipient)
                    
            else:
                lg.warn("%s: cannot parse line %s: %s"%(filename, lc,line))
        
        #remove keys without actual working rules
        for k in retdict.keys():
            if len(retdict[k])==0:
                del retdict[k]
        
        return retdict
    
    def lint(self):
        allok=(self.checkConfig() and self.lint_file())
        return allok
    
    def lint_file(self):
        configfile=self.config.get(self.section,'configfile')
        if not os.path.isfile(configfile):
            print "Config file %s does not exist"%configfile
            return False
        
        dic=self.load_file(configfile)
        for k,v in dic.iteritems():
            print "%s : %s rules"%(k,len(v))
            
        return True
    
    def __str__(self):
        return "RecipientRules Plugin"