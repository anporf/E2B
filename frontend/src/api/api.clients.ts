import { HTTP } from './api';
import { clientER, XML_API_CLIENT } from './clients';

export const XML_API = {
    validateExportXml: async (id?: string | number): Promise<{missing_fields: Array<any>}> => {
        try {
            console.log('Validating export XML for ID:', id);
            
            return {
                missing_fields: [
                    {
                        id: "c_1_2_date_creation",
                        label: "C.1.2 Date of Creation",
                        description: "Date when this report was first created",
                        type: "date"
                    },
                    {
                        id: "e_i_2_1b_reaction",
                        label: "E.i.2.1b Reaction/Event MedDRA term (PT)",
                        description: "MedDRA term for the reported reaction/event",
                        type: "select"
                    }
                ]
            };
        } catch (error) {
            console.error('Error validating export XML:', error);
            return { missing_fields: [] };
        }
    },
    
    exportMultipleXml: async (ids: Array<string | number>): Promise<Blob> => {
        try {
            console.log("Exporting XML for multiple ICSRs:", ids);
            
            const response = await XML_API_CLIENT.post('export-multiple', { 
                data: { ids },
                responseFormat: 'blob'
            });
            
            if (response instanceof Blob) {
                return response;
            }
            
            const responseText = typeof response === 'string' 
                ? response 
                : JSON.stringify(response);
            
            return new Blob([responseText], { type: 'application/xml' });
        } catch (error) {
            console.error('Error exporting multiple XML:', error);
            throw error;
        }
    },
    
    importMultipleXml: async (xmlContents: Array<string>): Promise<any> => {
        try {
            console.log("Importing multiple XML files, count:", xmlContents.length);
            
            const response = await XML_API_CLIENT.post('import-multiple', { 
                data: { files: xmlContents }
            });
            
            return response;
        } catch (error) {
            console.error('Error importing multiple XML files:', error);
            throw error;
        }
    }
};
